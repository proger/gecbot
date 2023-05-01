"""
This training script can be run both on a single gpu in debug mode,
and also in a larger training run with distributed data parallel (ddp).

To run on a single GPU, example:
$ python train.py --batch_size=32 --compile=False

To run with DDP on 4 gpus on 1 node, example:
$ torchrun --standalone --nproc_per_node=4 train.py

To run with DDP on 4 gpus across 2 nodes, example:
- Run on the first (master) node with example IP 123.456.123.456:
$ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=0 --master_addr=123.456.123.456 --master_port=1234 train.py
- Run on the worker node:
$ torchrun --nproc_per_node=8 --nnodes=2 --node_rank=1 --master_addr=123.456.123.456 --master_port=1234 train.py
(If your cluster does not have Infiniband interconnect prepend NCCL_IB_DISABLE=1)
"""

import os
import time
import math
from pathlib import Path
from typing import Dict, List, Optional
from contextlib import nullcontext

import numpy as np
import torch
from torch.nn.parallel import DistributedDataParallel as DDP
from torch.distributed import init_process_group, destroy_process_group

from model import GPTConfig, GPT
from lora import lora_find_and_replace, mark_only_lora_as_trainable, gpt2_peft_config, print_trainable_parameters


def construct_path_suffix(
    config: Dict,
    base_config: Dict,
    always_include: Optional[List[str]] = None,
    always_ignore: Optional[List[str]] = None,
) -> str:
    suffix_parts: List[str] = []
    if always_include is None:
        always_include = []
    if always_ignore is None:
        always_ignore = []

    for k in sorted(config.keys()):
        if k in always_ignore:
            continue
        if k in always_include or config[k] != base_config.get(k):
            suffix_parts.append(f"{k}-{str(config[k]).replace('.', '_').replace('/', '_')}")

    return ".".join(suffix_parts)


# -----------------------------------------------------------------------------
# default config values designed to train a gpt2 (124M) on OpenWebText
# I/O
init = str(Path.home() / "gpt/exp/uk4b_medium/ckpt.pt")
ckpt_path = "exp/gec_medium/ckpt.pt"
eval_interval = 100
log_interval = 1  # as many as grad acc steps
eval_iters = 200
eval_only = False  # if True, script exits right after the first eval
always_save_checkpoint = True  # if True, always save a checkpoint after each eval
# wandb logging
wandb_log = True  # disabled by default
wandb_project = "gecbot"
wandb_run_name = "lora"  # 'run' + str(time.time())
# data
gradient_accumulation_steps = 2  # used to simulate larger batch sizes
batch_size = 4  # if gradient_accumulation_steps > 1, this is the micro-batch size
block_size = 1024
train_bin = "gec_train_wiki.bin"
valid_bin = "gec_valid_wiki.bin"
# model
n_layer = 24
n_head = 16
n_embd = 1024
dropout = 0.0  # for pretraining 0 is good, for finetuning try 0.1+
bias = False  # do we use bias inside LayerNorm and Linear layers?
# adamw optimizer
learning_rate = 2e-4  # max learning rate
max_iters = 500 * 5  # total number of training iterations
weight_decay = 1e-2
beta1 = 0.9
beta2 = 0.99
grad_clip = 1.0  # clip gradients at this value, or disable if == 0.0
# learning rate decay settings
decay_lr = True  # whether to decay the learning rate
warmup_iters = max_iters // 5  # how many steps to warm up for
lr_decay_iters = max_iters  # should be ~= max_iters per Chinchilla
min_lr = 6e-5  # minimum learning rate, should be ~= learning_rate/10 per Chinchilla
# DDP settings
backend = "nccl"  # 'nccl', 'gloo', etc.
# system
device = "cuda"  # examples: 'cpu', 'cuda', 'cuda:0', 'cuda:1' etc., or try 'mps' on macbooks
dtype = "bfloat16"  # 'float32', 'bfloat16', or 'float16', the latter will auto implement a GradScaler
compile = True  # use PyTorch 2.0 to compile the model to be faster
# -----------------------------------------------------------------------------
config_keys = [k for k, v in globals().items() if not k.startswith("_") and isinstance(v, (int, float, bool, str))]
base_config = {k: globals()[k] for k in config_keys}  # will be used to find customized params
exec(open("configurator.py").read())  # overrides from command line or config file
config = {k: globals()[k] for k in config_keys}  # will be useful for logging


ckpt_path = Path(ckpt_path)
ckpt_suffix = construct_path_suffix(
    config,
    base_config,
    always_include=["init"],
    always_ignore=["ckpt_path", "train_bin", "valid_bin", "wandb_log", "wandb_project", "wandb_run_name", "compile"],
)
ckpt_path = ckpt_path.parent / f"{ckpt_path.stem}__{ckpt_suffix}{ckpt_path.suffix}"
print(f"Saving checkpoint to {ckpt_path}")
# -----------------------------------------------------------------------------

# various inits, derived attributes, I/O setup
ddp = int(os.environ.get("RANK", -1)) != -1  # is this a ddp run?
if ddp:
    init_process_group(backend=backend)
    ddp_rank = int(os.environ["RANK"])
    ddp_local_rank = int(os.environ["LOCAL_RANK"])
    device = f"cuda:{ddp_local_rank}"
    torch.cuda.set_device(device)
    master_process = ddp_rank == 0  # this process will do logging, checkpointing etc.
    seed_offset = ddp_rank  # each process gets a different seed
else:
    # if not ddp, we are running on a single gpu, and one process
    master_process = True
    seed_offset = 0

if master_process:
    ckpt_path.parent.mkdir(parents=True, exist_ok=True)
torch.manual_seed(1337 + seed_offset)
torch.backends.cuda.matmul.allow_tf32 = True  # allow tf32 on matmul
torch.backends.cudnn.allow_tf32 = True  # allow tf32 on cudnn
device_type = "cuda" if "cuda" in device else "cpu"  # for later use in torch.autocast
# note: float16 data type will automatically use a GradScaler
ptdtype = {"float32": torch.float32, "bfloat16": torch.bfloat16, "float16": torch.float16}[dtype]
ctx = nullcontext() if device_type == "cpu" else torch.amp.autocast(device_type=device_type, dtype=ptdtype)

# poor man's data loader
train_data = np.memmap(train_bin, dtype=np.uint16, mode="r")
val_data = np.memmap(valid_bin, dtype=np.uint16, mode="r")




def get_batch(split):
    data = train_data if split == "train" else val_data
    ix = torch.randint(len(data) - block_size, (batch_size,))
    x = torch.stack([torch.from_numpy((data[i : i + block_size]).astype(np.int64)) for i in ix])
    y = torch.stack([torch.from_numpy((data[i + 1 : i + 1 + block_size]).astype(np.int64)) for i in ix])
    if device_type == "cuda":
        # pin arrays x,y, which allows us to move them to GPU asynchronously (non_blocking=True)
        x, y = x.pin_memory().to(device, non_blocking=True), y.pin_memory().to(device, non_blocking=True)
    else:
        x, y = x.to(device), y.to(device)
    return x, y


# init these up here, can override if init_from='resume' (i.e. from a checkpoint)
iter_num = 0
best_val_loss = 1e9

# model init
model_args = dict(
    n_layer=n_layer, n_head=n_head, n_embd=n_embd, block_size=block_size, bias=bias, vocab_size=None, dropout=dropout
)  # start with model_args from command line

print(f"Resuming training from a checkpoint")
# resume training from a checkpoint.
checkpoint = torch.load(init, map_location=device)
checkpoint_model_args = checkpoint["model_args"]
# force these config attributes to be equal otherwise we can't even resume training
# the rest of the attributes (e.g. dropout) can stay as desired from command line
for k in ["n_layer", "n_head", "n_embd", "block_size", "bias", "vocab_size"]:
    if k == 'bias':
        model_args[k] = checkpoint_model_args.get(k, True)
        bias = model_args[k]
        print('bias ', model_args[k])
    elif k == 'vocab_size':
        model_args[k] = checkpoint_model_args.get(k, 50257)
        print('vocab size ', model_args[k])
    else:
        model_args[k] = checkpoint_model_args[k]
# create the model
gptconf = GPTConfig(**model_args)
model = GPT(gptconf)
state_dict = checkpoint["model"]
# fix the keys of the state dictionary :(
# honestly no idea how checkpoints sometimes get this prefix, have to debug more
unwanted_prefix = "_orig_mod."
for k, v in list(state_dict.items()):
    if k.startswith(unwanted_prefix):
        state_dict[k[len(unwanted_prefix) :]] = state_dict.pop(k)
if bias:
    model.load_state_dict(state_dict, strict=False)
else:
    model.load_state_dict(state_dict)

# crop down the model block size if desired, using model surgery
if block_size < model.config.block_size:
    model.crop_block_size(block_size)
    model_args["block_size"] = block_size  # so that the checkpoint will have the right value

# initialize a GradScaler. If enabled=False scaler is a no-op
scaler = torch.cuda.amp.GradScaler(enabled=(dtype == "float16"))

lora_find_and_replace(model, gpt2_peft_config)
mark_only_lora_as_trainable(model, gpt2_peft_config.bias)
print_trainable_parameters(model)
model.to(device)

# optimizer
optimizer = model.configure_optimizers(weight_decay, learning_rate, (beta1, beta2), device_type)
if False and init_from == "resume":
    optimizer.load_state_dict(checkpoint["optimizer"])

# compile the model
if compile:
    print("compiling the model... (takes a ~minute)")
    unoptimized_model = model
    model = torch.compile(model)  # requires PyTorch 2.0

# wrap model into DDP container
if ddp:
    model = DDP(model, device_ids=[ddp_local_rank])

# helps estimate an arbitrarily accurate loss over either split using many batches
@torch.no_grad()
def estimate_loss():
    out = {}
    model.eval()
    for split in ["train", "val"]:
        losses = torch.zeros(eval_iters)
        for k in range(eval_iters):
            X, Y = get_batch(split)
            with ctx:
                logits, loss = model(X, labels=Y)
            losses[k] = loss.item()
        out[split] = losses.mean()
    model.train()
    return out


# learning rate decay scheduler (cosine with warmup)
def get_lr(it):
    # 1) linear warmup for warmup_iters steps
    if it < warmup_iters:
        return learning_rate * it / warmup_iters
    # 2) if it > lr_decay_iters, return min learning rate
    if it > lr_decay_iters:
        return min_lr
    # 3) in between, use cosine decay down to min learning rate
    decay_ratio = (it - warmup_iters) / (lr_decay_iters - warmup_iters)
    assert 0 <= decay_ratio <= 1
    coeff = 0.5 * (1.0 + math.cos(math.pi * decay_ratio))  # coeff ranges 0..1
    return min_lr + coeff * (learning_rate - min_lr)


# logging
if wandb_log and master_process:
    import wandb

    wandb.init(project=wandb_project, name=wandb_run_name, config=config)

# training loop
X, Y = get_batch("train")  # fetch the very first batch
t0 = time.time()
while True:

    # determine and set the learning rate for this iteration
    lr = get_lr(iter_num) if decay_lr else learning_rate
    for param_group in optimizer.param_groups:
        param_group["lr"] = lr

    # evaluate the loss on train/val sets and write checkpoints
    if iter_num % eval_interval == 0 and master_process:
        losses = estimate_loss()
        print(f"step {iter_num}: train loss {losses['train']:.4f}, val loss {losses['val']:.4f}")
        if wandb_log:
            wandb.log(
                {
                    "iter": iter_num,
                    "train/loss": losses["train"],
                    "val/loss": losses["val"],
                    "lr": lr,
                }
            )
        if not math.isnan(losses["val"]):
            best_val_loss = losses["val"]
            raw_model = model.module if ddp else model
            checkpoint = {
                "model": raw_model.state_dict(),
                "optimizer": optimizer.state_dict(),
                "model_args": model_args,
                "iter_num": iter_num,
                "best_val_loss": best_val_loss,
                "val_loss": losses['val'],
                "config": config,
            }
            if losses['val'] < best_val_loss:
                best_val_loss = losses["val"]
                checkpoint["best_val_loss"] = best_val_loss
                if iter_num > 0:
                    torch.save(checkpoint, ckpt_path)
            if always_save_checkpoint:
                torch.save(checkpoint, ckpt_path.with_suffix(".last.pt"))
        else:
            print("NaN loss detected")
            break
    if iter_num == 0 and eval_only:
        break

    # forward backward update, with optional gradient accumulation to simulate larger batch size
    # and using the GradScaler if data type is float16
    for micro_step in range(gradient_accumulation_steps):
        if ddp:
            # in DDP training we only need to sync gradients at the last micro step.
            # the official way to do this is with model.no_sync() context manager, but
            # I really dislike that this bloats the code and forces us to repeat code
            # looking at the source of that context manager, it just toggles this variable
            model.require_backward_grad_sync = micro_step == gradient_accumulation_steps - 1
        with ctx:
            logits, loss = model(X, labels=Y)
        # immediately async prefetch next batch while model is doing the forward pass on the GPU
        X, Y = get_batch("train")
        # backward pass, with gradient scaling if training in fp16
        if torch.isnan(loss):
            break
        scaler.scale(loss).backward()
    if torch.isnan(loss):
        print("loss is NaN, skipping this update")
        continue
    # clip the gradient
    if grad_clip != 0.0:
        scaler.unscale_(optimizer)
        torch.nn.utils.clip_grad_norm_(model.parameters(), grad_clip)
    # step the optimizer and scaler if training in fp16
    scaler.step(optimizer)
    scaler.update()
    # flush the gradients as soon as we can, no need for this memory anymore
    optimizer.zero_grad(set_to_none=True)

    # timing and logging
    t1 = time.time()
    dt = t1 - t0
    t0 = t1
    if iter_num % log_interval == 0 and master_process:
        lossf = loss.item()  # loss as float. note: this is a CPU-GPU sync point
        print(f"iter {iter_num}: loss {lossf:.4f}, time {dt*1000:.2f}ms, lr={lr:.6f}")
    iter_num += 1

    # termination conditions
    if iter_num > max_iters:
        break

if ddp:
    destroy_process_group()
