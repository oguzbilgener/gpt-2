#!/usr/bin/env python3
# Usage:
#  PYTHONPATH=src ./train --dataset <file|directory|glob>
import os
import random
import sys
sys.path += [os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')]

import argparse
import json
import numpy as np
import tensorflow as tf
import time
import tqdm
import math
from tensorflow.core.protobuf import rewriter_config_pb2
from tensorflow.core.protobuf import config_pb2
from tensorflow.python import pywrap_tensorflow
from tensorflow.python.framework.errors_impl import InvalidArgumentError, AbortedError, DeadlineExceededError

import model, sample, encoder
from load_dataset import load_dataset, Sampler, TextSampler
from accumulate import AccumulatingOptimizer
import memory_saving_gradients
from glob import glob
import re
import tflex
import tflex_sgdr
import tflex_optimizers

import pytz
from datetime import datetime, timezone

import threading
from collections import defaultdict

CHECKPOINT_DIR = 'checkpoint'
SAMPLE_DIR = 'samples'


parser = argparse.ArgumentParser(
    description='Fine-tune GPT-2 on your custom dataset.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter)

parser.add_argument('--dataset', metavar='PATH', type=str, required=True, help='Input file, directory, or glob pattern (utf-8 text, or preencoded .npz files).')
parser.add_argument('--model_name', metavar='MODEL', type=str, default='117M', help='Pretrained model name')
parser.add_argument('--combine', metavar='CHARS', type=int, default=50000, help='Concatenate input files with <|endoftext|> separator into chunks of this minimum size')

parser.add_argument('--batch_size', metavar='SIZE', type=int, default=1, help='Batch size')
parser.add_argument('--learning_rate', metavar='LR', type=float, default=0.00015, help='Learning rate for Adam')
parser.add_argument('--learning_rate_min', type=float, default=0.00001, help='Minimum learning rate')
parser.add_argument('--learning_rate_cos', default=False, action='store_true', help='Use learn rate cosine annealing')
parser.add_argument('--learning_rate_warmup', type=int, default=100, help='Learning rate warmup for cosine annealing')
parser.add_argument('--learning_rate_period', type=int, default=100, help='Learning rate period for cosine annealing')
parser.add_argument('--learning_rate_initial_step', type=int, default=0, help='Learning rate initial step for cosine annealing')
parser.add_argument('--accumulate_gradients', metavar='N', type=int, default=1, help='Accumulate gradients across N minibatches.')
parser.add_argument('--memory_saving_gradients', default=False, action='store_true', help='Use gradient checkpointing to reduce vram usage.')
parser.add_argument('--only_train_transformer_layers', default=False, action='store_true', help='Restrict training to the transformer blocks.')
parser.add_argument('--optimizer', type=str, default='adamw', help='Optimizer. <adam|adamw|sgd|ada>.')
parser.add_argument('--weight_decay', metavar='WD', type=float, default=1e-4, help='Weight decay for AdamW/AdaW')
parser.add_argument('--noise', type=float, default=0.0, help='Add noise to input training data to regularize against typos.')

parser.add_argument('--top_k', type=int, default=40, help='K for top-k sampling.')
parser.add_argument('--top_p', type=float, default=0.0, help='P for top-p sampling. Overrides top_k if set > 0.')

parser.add_argument('--restore_from', type=str, default='latest', help='Either "latest", "fresh", or a path to a checkpoint file')
parser.add_argument('--run_name', type=str, default='run1', help='Run id. Name of subdirectory in checkpoint/ and samples/')
parser.add_argument('--sample_every', metavar='N', type=int, default=100, help='Generate samples every N steps')
parser.add_argument('--sample_length', metavar='TOKENS', type=int, default=-1, help='Sample this many tokens')
parser.add_argument('--sample_num', metavar='N', type=int, default=1, help='Generate this many samples')
parser.add_argument('--save_every', metavar='N', type=int, default=-1, help='Write a checkpoint every N steps')
parser.add_argument('--save_time', metavar='N', type=float, default=15.0, help='Write a checkpoint every N minutes')
parser.add_argument('--max_to_keep', metavar='N', type=int, default=5, help='Only keep the last N checkpoints')

parser.add_argument('--val_dataset', metavar='PATH', type=str, default=None, help='Dataset for validation loss, defaults to --dataset.')
parser.add_argument('--val_batch_size', metavar='SIZE', type=int, default=1, help='Batch size for validation.')
parser.add_argument('--val_batch_count', metavar='N', type=int, default=80, help='Number of batches for validation.')
parser.add_argument('--val_every', metavar='STEPS', type=int, default=0, help='Calculate validation loss every STEPS steps.')

parser.add_argument('--init_tpu', default=False, action='store_true', help='Initialize TPU session.')

parser.add_argument('--fresh_model', default=False, action='store_true', help="Don't load model from disk; initialize model weights to random values")
parser.add_argument('--save_on_ctrlc', default=False, action='store_true', help='When execution is interrupted, should we save the model to disk?')
parser.add_argument('--debug_on_ctrlc', default=False, action='store_true', help='When execution is interrupted, attach a debugger (pdb.set_trace())')
parser.add_argument('--float16', default=False, action='store_true', help='Use float16 weights?')
parser.add_argument('--dtype', type=str, default='float32', help='dtype. <float32|float16|bfloat16>.')

parser.add_argument('--targets', type=str, default='', help='')

# 1.5B
#parser.add_argument('--n_ctx', type=int, default=1024, help='For a fresh model, how large should n_ctx be?')
#parser.add_argument('--n_embd', type=int, default=1600, help='For a fresh model, how large should n_embd be?')
#parser.add_argument('--n_head', type=int, default=25, help='For a fresh model, how large should n_head be?')
#parser.add_argument('--n_layer', type=int, default=48, help='For a fresh model, how large should n_layer be?')

# 345M
#parser.add_argument('--n_ctx', type=int, default=1024, help='For a fresh model, how large should n_ctx be?')
#parser.add_argument('--n_embd', type=int, default=1024, help='For a fresh model, how large should n_embd be?')
#parser.add_argument('--n_head', type=int, default=16, help='For a fresh model, how large should n_head be?')
#parser.add_argument('--n_layer', type=int, default=24, help='For a fresh model, how large should n_layer be?')

# 117M
#parser.add_argument('--n_ctx', type=int, default=1024, help='For a fresh model, how large should n_ctx be?')
#parser.add_argument('--n_embd', type=int, default=768, help='For a fresh model, how large should n_embd be?')
#parser.add_argument('--n_head', type=int, default=12, help='For a fresh model, how large should n_head be?')
#parser.add_argument('--n_layer', type=int, default=12, help='For a fresh model, how large should n_layer be?')

parser.add_argument('--n_ctx', type=int, default=-1, help='For a fresh model, how large should n_ctx be?')
parser.add_argument('--n_embd', type=int, default=-1, help='For a fresh model, how large should n_embd be?')
parser.add_argument('--n_head', type=int, default=-1, help='For a fresh model, how large should n_head be?')
parser.add_argument('--n_layer', type=int, default=-1, help='For a fresh model, how large should n_layer be?')

parser.add_argument('--sample_ctx', type=int, default=-1, help='Compute loss over N samples. Equal to n_ctx if set < 0.')

parser.add_argument('--truncate_weights', default=False, action='store_true', help="Try loading variables from snapshots, even if those variables' shapes do not match")

parser.add_argument('--debug_print_all_vars', default=False, action='store_true', help="Print all variables after running one training step")
parser.add_argument('--debug_print_trainable_vars', default=False, action='store_true', help="Print trainable variables after running one training step")

parser.add_argument('--allow_growth', default=False, action='store_true', help="Set config.gpu_options.allow_growth = True")
parser.add_argument('--allow_soft_placement', default=False, action='store_true', help="Set config.gpu_options.allow_soft_placement = True")
parser.add_argument('--disable_layout_optimizer', default=False, action='store_true', help="Set config.graph_options.rewrite_options.layout_optimizer = rewriter_config_pb2.RewriterConfig.OFF")

parser.add_argument('--debug_before_training', default=False, action='store_true', help="Drop into debugger before starting the training loop")

parser.add_argument('--dropout', type=float, default=0.0, help="Dropout value. Disabled if set <= 0.0. For training on large datasets, 0.1 tends to be a good value.")

parser.add_argument('--seed', type=int, default=-1, help='Deterministic seed for dataset sampler. Disabled if set < 0')

parser.add_argument('--save_graph', default=False, action='store_true', help="Save TensorFlow graph to summary log (to see ops in tensorboard)")

parser.add_argument('--device', type=int, default=-1, help='device to use.')

PST = pytz.timezone('US/Pacific')

def timestamp(now=None, tz=None):
    if now is None:
        now = datetime.now(timezone.utc)
    if tz is None:
        tz = PST
    return "{}".format(now.astimezone(tz).isoformat())

def maketree(path):
    try:
        os.makedirs(path)
    except:
        pass


def randomize(context, hparams, p):
    if p > 0:
        mask = tf.random.uniform(shape=tf.shape(context)) < p
        noise = tf.random.uniform(shape=tf.shape(context), minval=0, maxval=hparams.n_vocab, dtype=tf.int32)
        return tf.where(mask, noise, context)
    else:
        return context

class TrainCounter(object):
  def __init__(self, value=0):
    self.value = value
    self.lock = threading.Lock()

  def incr(self, n=1):
    try:
      self.lock.acquire()
      self.value += n
      return self.value
    finally:
      self.lock.release()

tflex.pinned_sessions = []
tflex.session_timeout_in_ms = 600000
tflex.eval_lightweight_timeout = 10000
tflex.load_lightweight_timeout = 10000
tflex.initialize_timeout = 30000
tflex.context_load_timeout = 10000
tflex.ensure_on_init = True
tflex.release_trainer_sema = True
tflex.tpu_init_timeout = 10000
tflex.use_global_data_sampler = False
tflex.shuffle_cycles = True

def eval_lightweight(variable, session, timeout_in_ms=None):
  if timeout_in_ms is None:
    timeout_in_ms = tflex.eval_lightweight_timeout
  return tflex.eval(variable, session=session, timeout_in_ms=tflex.eval_lightweight_timeout)

def load_lightweight(variable, value, session, timeout_in_ms=None):
  if timeout_in_ms is None:
    timeout_in_ms = tflex.load_lightweight_timeout
  return tflex.load(variable, value, session=session, timeout_in_ms=timeout_in_ms)

class TrainGPT2(threading.Thread):
  def __init__(self, args, hparams, sampler, enc, scope='model', target='auto', timeout=tflex.session_timeout_in_ms, session=None, counter=None):
    super(TrainGPT2, self).__init__()
    self.fresh = True
    self.dead = False
    self.args = args
    self.hparams = hparams
    self.sampler = sampler
    self.target = target
    self.enc = enc
    if session is None:
      config = config_pb2.ConfigProto(operation_timeout_in_ms=timeout)
      self.timeout = timeout
      config.allow_soft_placement = False
      if args.allow_soft_placement:
          config.allow_soft_placement = True
      if args.allow_growth:
          config.gpu_options.allow_growth = True
      if args.disable_layout_optimizer:
          config.graph_options.rewrite_options.layout_optimizer = rewriter_config_pb2.RewriterConfig.OFF
      session = tflex.Session(target=target, config=config, init_tpu=args.init_tpu)
      tflex.pinned_sessions.append([target, session]) # prevent GC'ing sessions, because the destructor seems to freeze.
    if args.init_tpu:
      print('Initializing TPU...', self.target)
      session.run(tf.contrib.tpu.initialize_system(), options=config_pb2.RunOptions(timeout_in_ms=tflex.tpu_init_timeout))

    #cores = session.list_devices()[2:]
    #core = cores[args.device].name if len(cores) > 0 and args.device >= 0 else None
    #with tf.device(core):
    if True:
      #context = tf.placeholder(tf.int32, [args.batch_size, None])
      context = tf.Variable(tf.zeros(shape=[args.batch_size, args.sample_ctx], dtype=tf.int32), dtype=tf.int32, name="context", trainable=False)
      context_in = randomize(context, hparams, args.noise)
      output = model.model(hparams=hparams, X=context_in, scope=scope)
      loss = tf.reduce_mean(
        tf.nn.sparse_softmax_cross_entropy_with_logits(
          labels=context[:, 1:], logits=output['logits'][:, :-1]))

    with tf.variable_scope(tf.get_variable_scope().name, reuse=tf.AUTO_REUSE):
      global_step = tflex.get_variable('global_step') or tf.get_variable('global_step', shape=(), dtype=tf.int32, trainable=False)
      current_step = counter
      #load_lightweight(global_step, current_step.value, session=session)
      if args.learning_rate_cos:
          lr = tflex_sgdr.sgdr_decay_with_warmup(args.learning_rate, global_step,
              warmup_steps=args.learning_rate_warmup, initial_period_steps=args.learning_rate_period, learning_rate_min=args.learning_rate_min)
      else:
          lr = tflex.get_variable('learn_rate') or tf.get_variable('learn_rate', shape=(), dtype=tf.float32, trainable=False)
          #load_lightweight(lr,args.learning_rate, session=session)
      wd = tflex.get_variable('weight_decay') or tf.get_variable('weight_decay', shape=(), dtype=tf.float32, trainable=False)

      use_locking=False
      if args.optimizer == 'adam':
        opt = tf.train.AdamOptimizer(learning_rate=lr, use_locking=use_locking)
      elif args.optimizer == 'adamw':
        opt = tflex_optimizers.AdamWOptimizer(learning_rate=lr, use_locking=use_locking, weight_decay=wd)
      elif args.optimizer == 'sgd':
        opt = tf.train.GradientDescentOptimizer(learning_rate=lr, use_locking=use_locking)
      elif args.optimizer == 'ada':
        import tensor2tensor.utils.optimize
        from tensor2tensor.utils import hparam
        import tensor2tensor.models.research
        from tensor2tensor.utils import registry
        ada_hparams = registry.hparams('afx_mimic_adam')
        ada_hparams.optimizer_adafactor_beta1 = 0.0
        ada_hparams.optimizer_adafactor_factored = True
        opt = tensor2tensor.utils.optimize.adafactor(learning_rate=lr, hparams=ada_hparams)
      elif args.optimizer == 'adaw':
        opt = tflex_optimizers.AdafactorWOptimizer(learning_rate=lr, use_locking=use_locking, weight_decay=wd)
      else:
        exit('Bad optimizer:', args.optimizer)

      all_vars = [v for v in tf.trainable_variables() if v.name.startswith(scope + '/')]
      train_vars = [v for v in all_vars if '/h' in v.name or '/ln_f' in v.name] if args.only_train_transformer_layers else all_vars

      parameter_count = sum([np.prod(v.shape.as_list()) for v in train_vars])
      print("This model is using %d parameters (%.2fM)" % (parameter_count, parameter_count/(1024.0*1024.0)))

      opt_grads = tf.gradients(loss, train_vars)
      opt_grads = list(zip(opt_grads, train_vars))
      opt_apply = opt.apply_gradients(opt_grads)
      summary_loss = tf.summary.scalar('loss', loss)
      summary_perp = tf.summary.scalar('perplexity', tf.math.exp(loss))
      global_vars = [v for v in tf.global_variables() if v.name.startswith(scope + '/')]
      fetch_global_vars = list(tflex.split_by_params(global_vars))
      fetch_train_vars = list(tflex.split_by_params(train_vars))
        #fetch_vars = list(tflex.split_by_params(all_vars))

      summary_lr = tf.summary.scalar('learning_rate', lr)
      summary_wd = tf.summary.scalar('weight_decay', wd)
      summaries = tf.summary.merge([summary_lr, summary_wd, summary_loss, summary_perp])
      run_name = args.run_name + "_" + self.target
      run_name = run_name.replace('/', '_').replace(':', '_').replace('.', '_')
      self.summary_log = tf.summary.FileWriter(os.path.join(CHECKPOINT_DIR, run_name))
      self.summaries = summaries
      self.loss = loss
      self.context = context
      self.output = output
      self.opt = opt
      self.all_vars = all_vars
      self.train_vars = train_vars
      self.global_vars = global_vars
      self.fetch_global_vars = fetch_global_vars
      self.fetch_train_vars = fetch_train_vars
      self.fetch_vars = self.fetch_train_vars if args.optimizer in ['adam', 'adamw'] else self.fetch_global_vars
      self.opt_grads = opt_grads
      self.opt_apply = opt_apply
      self.sess = session
      self.lr = lr
      self.wd = wd
      self.counter = current_step.incr()
      self.stopped = False
      self.paused = False
      self.current_step = current_step
      self.global_step = global_step
      self.saver = tflex.Saver(
            var_list=all_vars,
            max_to_keep=args.max_to_keep,
            keep_checkpoint_every_n_hours=2,
            reshape=args.truncate_weights)
      self.init = tf.global_variables_initializer()
      self.avg_loss = [0.0, 0.0]
      self.avg_perp = [0.0, 0.0]
    self.start_time = time.time()
    self.prev_time = self.start_time
    
  def aborted(self):
    try:
      self.sess.list_devices()
      return False
    except InvalidArgumentError:
      return True
    except AbortedError:
      return True
    except DeadlineExceededError:
      return True

  def sample_batch(self):
    args = self.args
    return [self.sampler.sample(args.sample_ctx) for _ in range(args.batch_size)]
  
  def elapsed(self):
    return time.time() - self.start_time

  def say(self, msg):
    print('{stamp} {target:16s} [{counter} | {time:2.4f}] {msg}'.format(stamp=timestamp(), target=self.target[-16:], counter=self.counter, time=self.elapsed(), msg=msg))

  def update_lr(self, step=None, rate=None):
    global_step = self.global_step
    args = self.args
    lr = self.lr
    wd = self.wd
    sess = self.sess
    weight_decay = args.weight_decay
    if not args.learning_rate_cos:
      if step is None:
        step = eval_lightweight(global_step, session=sess)
      if rate is None:
        rate = args.learning_rate
      if callable(rate):
        rate = rate(step)
      load_lightweight(lr, rate, session=sess)
    load_lightweight(wd, weight_decay, session=sess)
    v_rate = eval_lightweight(lr, session=sess)
    v_weight_decay = eval_lightweight(wd, session=sess)
    return v_rate, v_weight_decay
  
  def run(self):
    while not self.stopped:
      while self.paused:
        time.sleep(0.1)
      self.fit()
      time.sleep(0.1)

  def ensure(self):
    if self.init is not None:
      args = self.args
      self.say('Initializing...')
      self.sess.run(self.init, options=config_pb2.RunOptions(timeout_in_ms=tflex.initialize_timeout))
      if not args.fresh_model:
        tflex.load_trainer(self)
      self.say('Initialized.')
      self.init = None

  def fit(self):
    self.ensure()
    load_lightweight(self.global_step, self.counter, session=self.sess)
    v_rate, v_weight_decay = self.update_lr()
    self.say('Generating batch...')
    batch = self.sample_batch()
    print(repr(self.enc.decode(batch[0]))[0:150] + '...')
    self.say('Loading context...')
    load_lightweight(self.context, batch, session=self.sess, timeout_in_ms=tflex.context_load_timeout)
    self.say('Running opt_apply...')
    (_, v_loss, v_summary) = self.sess.run((self.opt_apply, self.loss, self.summaries), options=config_pb2.RunOptions(timeout_in_ms=self.timeout))
    self.avg_loss = [self.avg_loss[0] * 0.99 + v_loss,
                     self.avg_loss[1] * 0.99 + 1.0]
    v_perp = math.exp(v_loss)
    self.avg_perp = [self.avg_perp[0] * 0.99 + v_perp,
                     self.avg_perp[1] * 0.99 + 1.0]
    now = time.time()
    print('{stamp} {target:16s} [{counter} | {time:2.4f} | {delta:2.2f}s | {ops:2.6f}tokens/s] loss={loss:2.4f} perp={perp:2.4f} avgloss={avgloss:2.4f} avgperp={avgperp:2.4f} rate={rate:0.7f} decay={decay:0.7f} step={step}'
        .format(
            stamp=timestamp(),
            target=self.target[-16:],
            counter=self.counter,
            time=now - self.start_time,
            delta=now - self.prev_time,
            ops=self.args.sample_ctx * self.args.batch_size / (now - self.prev_time),
            rate=v_rate,
            decay=v_weight_decay,
            loss=v_loss,
            perp=v_perp,
            avgloss=self.avg_loss[0] / self.avg_loss[1],
            avgperp=self.avg_perp[0] / self.avg_perp[1],
            step=self.counter,
            ))
    self.prev_time = now
    self.summary_log.add_summary(v_summary, self.counter)
    self.summary_log.flush()
    self.counter = self.current_step.incr()
    self.start_count = self.counter
    #load_lightweight(self.global_step, self.counter, session=self.sess)

    return v_loss

  def variables(self, index):
    return tflex.cast_variables(self.fetch_vars[index % len(self.fetch_vars)], graph=self.sess.graph)

def trainer_starting(trainer):
  if trainer.init:
    return True
  return False

tflex.trainer_starting = trainer_starting

def trainer_alive(trainer):
  if tflex.trainer_starting(trainer):
    return False
  if hasattr(trainer, "dead"):
    if trainer.dead:
      return False
  if not trainer.is_alive():
    return False
  return True

tflex.trainer_alive = trainer_alive

def trainer_fresh(trainer):
  return trainer_starting(trainer) or trainer.fresh

tflex.trainer_fresh = trainer_fresh

def reset_trainer_stats(trainer):
  if not tflex.trainer_alive(trainer):
    return False
  x = trainer
  x.avg_loss[0] = x.avg_loss[1] = x.avg_perp[0] = x.avg_perp[1] = 0.0
  x.start_time = time.time()
  x.prev_time = x.start_time
  x.start_count = x.counter
  return True

tflex.reset_trainer_stats = reset_trainer_stats

def resume_trainer(trainer):
  if not tflex.trainer_alive(trainer):
    return False
  trainer.paused = False
  return True

tflex.resume_trainer = resume_trainer

def load_trainer(trainer, ckpt=None, reset_stats=True):
  args = trainer.args
  counter = trainer.counter
  saver = trainer.saver
  sess = trainer.sess
  trainer.say('Restoring...')
  if ckpt is None:
    if args.restore_from == 'latest':
      ckpt = tflex.latest_checkpoint(os.path.join(CHECKPOINT_DIR, args.run_name))
      if ckpt is None:
        # Get fresh GPT weights if new run.
        ckpt = tflex.latest_checkpoint(os.path.join('models', args.model_name))
    elif args.restore_from == 'fresh':
      ckpt = tflex.latest_checkpoint(os.path.join('models', args.model_name))
    else:
      ckpt = tflex.latest_checkpoint(args.restore_from)
  print('Loading snapshot %s...' % ckpt)
  t0 = time.time()
  saver.restore(sess, ckpt)
  t1 = time.time()
  print('Loaded in %f seconds' % (t1 - t0))
  if reset_stats:
    tflex.reset_trainer_stats(trainer)

tflex.load_trainer = load_trainer

def load_trainers(trainers=None, timeout=None):
  if trainers is None:
    trainers = list(tflex.get_trainers())
  trainers = [x for x in trainers if tflex.trainer_alive(x)]
  if timeout is None:
    timeout = len(trainers) * 30.0
  print('Loading %d trainers, max timeout %f' % (len(trainers), timeout))
  start_time = time.time()
  for thread in tqdm.tqdm(parallelize(trainers, tflex.load_trainer)):
    elapsed = (time.time() - start_time)
    waiting = timeout - elapsed
    if waiting > 0:
      thread.join(timeout=waiting)

tflex.load_trainers = load_trainers

def avgperp(trainer):
  return trainer.avg_perp[0] / (trainer.avg_perp[1] or 1.0)

tflex.avgperp = avgperp

def avgloss(trainer):
  return trainer.avg_loss[0] / (trainer.avg_loss[1] or 1.0)

tflex.avgloss = avgloss

def sorted_trainers(trainers=None):
  if trainers is None:
    trainers = [x for x in tflex.get_trainers()]
  return list(sorted(trainers, key=tflex.avgloss))

tflex.sorted_trainers = sorted_trainers

def print_trainer(x):
  ticks = 'ticks=%2.3f' % x.avg_loss[1]
  avgl = 'loss=%2.3f' % tflex.avgloss(x)
  avgp = 'perp=%2.3f' % tflex.avgperp(x)
  elapsed = 'elapsed=%ds' % int(x.prev_time - x.start_time)
  start = 'start=%d' % int(x.start_time)
  paused = 'paused=%s' % repr(x.paused)
  fresh = 'fresh=%s' % repr(tflex.trainer_fresh(x))
  alive = 'alive=%s' % repr(tflex.trainer_alive(x))
  print(x.target, start, paused, fresh, alive, elapsed, avgl, avgp, ticks);
  return x

tflex.print_trainer = print_trainer

@tflex.register_command
def print_trainers(trainers=None):
  if trainers is None:
    trainers = list(tflex.get_trainers())
  trainers = [x for x in trainers if tflex.trainer_alive(x)]
  for x in tflex.sorted_trainers(trainers)[::-1]:
    tflex.print_trainer(x)
  print(len([x for x in trainers if not tflex.trainer_fresh(x) and tflex.trainer_alive(x)]), "trainers")

tflex.print_trainers = print_trainers

def save_trainer(trainer):
  if not tflex.trainer_alive(trainer) or tflex.trainer_fresh(trainer):
    return False
  args = trainer.args
  counter = trainer.counter
  saver = trainer.saver
  sess = trainer.sess
  maketree(os.path.join(CHECKPOINT_DIR, trainer.args.run_name))
  print('Saving', os.path.join(CHECKPOINT_DIR, trainer.args.run_name, 'model-{}').format(counter))
  t0 = time.time()
  saver.save(sess, os.path.join(CHECKPOINT_DIR, args.run_name, 'model'), global_step=counter)
  t1 = time.time()
  print('Saved in %f seconds' % (t1 - t0))
  counter_path = os.path.join(CHECKPOINT_DIR, args.run_name, 'counter')
  with open(counter_path, 'w') as fp:
      fp.write(str(counter) + '\n')
  return True

tflex.save_trainer = save_trainer

def rank_trainers(trainers=None):
  if trainers is None:
    trainers = [x for x in tflex.get_trainers()]
  return list(sorted(trainers, key=lambda x: x.avg_loss[1], reverse=True))

tflex.rank_trainers = rank_trainers

def save_trainers(trainers=None):
  for trainer in tflex.rank_trainers(trainers):
    print('-----')
    print('Saving:')
    print_trainer(trainer)
    print('-----')
    if save_trainer(trainer):
      print('-----')
      print_trainer(trainer)
      print('Saved')
      print('-----')
      return True
  return False

tflex.save_trainers = save_trainers

@tflex.register_command
def save_lowest_loss(trainers=None):
  for trainer in tflex.sorted_trainers(trainers):
    print('-----')
    print('Saving:')
    print_trainer(trainer)
    print('-----')
    if save_trainer(trainer):
      print('-----')
      print_trainer(trainer)
      print('Saved')
      print('-----')
      return True
  return False

tflex.save_trainers = save_lowest_loss

def parallelize(xs, thunk, *args):
  threads = []
  for x in xs:
    thread = threading.Thread(target=thunk, args=(x, *args))
    thread.start()
    threads.append(thread)
  return threads

#tflex.read_deadline = 20000
#tflex.write_deadline = 20000
tflex.read_deadline = 30000
tflex.write_deadline = 30000

def assign_values(variables, values, session=None, timeout_in_ms=tflex.write_deadline):
  session = session or tf.get_default_session()
  ops = [x.initializer for x in variables]
  vals = dict([(x.initializer.inputs[1], value) for x, value in zip(variables, values)])
  #for x, (k, v) in zip(variables, vals.items()):
  #  print(x.name, x.shape.as_list(), k, v.shape)
  session.run(ops, vals, options=config_pb2.RunOptions(timeout_in_ms=timeout_in_ms))

def update_trainers(trainers, i, sync_all=False, timeout=30):
  trainers = [x for x in trainers]
  if len(trainers) <= 0:
    return
  #trainers = [x for x in all_trainers if not x.aborted()]
  #print('Fetching...')
  accum = {}
  accumcount = defaultdict(int)
  lock = threading.Lock()
  threads = []
  for trainer in trainers:
    if tflex.trainer_fresh(trainer):
      continue
    def thunk(trainer, lock, index):
      for variables in ([trainer.variables(index=index)] if not sync_all else tqdm.tqdm(list(tflex.split_by_params(trainer.global_vars)))):
        values = trainer.sess.run(variables, options=config_pb2.RunOptions(timeout_in_ms=tflex.read_deadline))
        try:
          lock.acquire()
          for variable, value in zip(variables, values):
            if variable.name in accum:
              accum[variable.name] = accum[variable.name] + value
            else:
              accum[variable.name] = value
            accumcount[variable.name] += 1
        finally:
          lock.release()
    thread = threading.Thread(target=thunk, args=(trainer,lock,i,))
    thread.start()
    threads.append(thread)
  start_time = time.time()
  for thread in threads:
    elapsed = (time.time() - start_time)
    waiting = timeout - elapsed
    if waiting > 0:
      thread.join(timeout=waiting)
  #print('Synchronizing...')
  threads = []
  for trainer in trainers:
    def thunk(trainer, index):
      for variables in ([trainer.variables(index=index)] if not sync_all else tqdm.tqdm(list(tflex.split_by_params(trainer.global_vars)))):
        values = []
        for v in variables:
          with lock:
            assert(v.name in accum)
            value = accum[v.name]
            n = accumcount[v.name]
            #assert(n > 0)
          if n > 0:
            values.append(value / n)
        #tflex.assign_values(variables, values, session=trainer.sess)
        assign_values(variables, values, session=trainer.sess, timeout_in_ms=tflex.write_deadline)
        #trainer.fresh = False
        #trainer.avg_loss[0] = avg_loss[0] / avg_count
        #trainer.avg_loss[1] = avg_loss[1] / avg_count
        #trainer.avg_perp[0] = avg_perp[0] / avg_count
        #trainer.avg_perp[1] = avg_perp[1] / avg_count
    thread = threading.Thread(target=thunk, args=(trainer,i,))
    thread.start()
    threads.append(thread)
  start_time = time.time()
  for thread in threads:
    elapsed = (time.time() - start_time)
    waiting = timeout - elapsed
    if waiting > 0:
      thread.join(timeout=waiting)
  #print('Synchronized.')

tflex.update_trainers = update_trainers

def main():
    args = parser.parse_args()
    enc = encoder.get_encoder(args.model_name)
    hparams = model.default_hparams()
    hparams.res_dropout = args.dropout
    hparams.attn_dropout = args.dropout
    epsilon = -1e10
    if args.dtype == 'float32':
        hparams.dtype = tf.float32
    elif args.dtype == 'float16':
        hparams.dtype = tf.float16
        epsilon = -65500
    elif args.dtype == 'bfloat16':
        hparams.dtype = tf.bfloat16
        epsilon = -65500
    else:
        print('Unknown dtype', args.dtype)
    if args.float16:
        hparams.dtype = tf.bfloat16
        epsilon = -65500

    with open(os.path.join('models', args.model_name, 'hparams.json')) as f:
        hparams.override_from_dict(json.load(f))
    if args.n_ctx >= 0:
        hparams.n_ctx=args.n_ctx
    if args.n_embd >= 0:
        hparams.n_embd=args.n_embd
    if args.n_head >= 0:
        hparams.n_head=args.n_head
    if args.n_layer >= 0:
        hparams.n_layer=args.n_layer

    if args.sample_length < 0:
        args.sample_length = hparams.n_ctx - 1
    if args.sample_length > hparams.n_ctx:
        raise ValueError(
            "Can't get samples longer than window size: %s" % hparams.n_ctx)
    if args.sample_ctx < 0:
      args.sample_ctx = hparams.n_ctx

    if args.model_name == '345M':
        args.memory_saving_gradients = True
        if args.optimizer == 'adam':
            args.only_train_transformer_layers = True

    def make_sampler(dataset, enc, seed, combine):
      if os.path.isdir(dataset) or dataset.endswith('.npz'):
        chunks = load_dataset(enc, dataset, combine)
        data_sampler = Sampler(chunks, seed=seed)
        print('dataset has', data_sampler.total_size, 'tokens', len(chunks), 'chunks')
      else:
        data_sampler = TextSampler(dataset, enc, seed=seed, use_locking=True)
      return data_sampler

    print('Loading dataset...')
    seed = None if args.seed < 0 else args.seed
    data_sampler = make_sampler(dataset=args.dataset, enc=enc, seed=seed, combine=args.combine)

    print('Training...')
    counter = 1
    counter_path = os.path.join(CHECKPOINT_DIR, args.run_name, 'counter')
    if os.path.exists(counter_path):
        # Load the step number if we're resuming a run
        # Add 1 so we don't immediately try to save again
        with open(counter_path, 'r') as fp:
            counter = int(fp.read()) + 1

    local = threading.local()

    targets = [x.strip() for x in args.targets.split(',') if len(x.strip()) > 0]
    if len(targets) <= 0:
      targets.append('auto')
    random.shuffle(targets)
    tflex.targets = targets
    traincounter = TrainCounter(value=counter)
    tflex.trainers = []
    tflex.pending_trainers = []
    tflex.pinned_trainers = []
    tflex.trainers_sema = threading.BoundedSemaphore(value=3)
    tflex.trainers_init_sema = threading.BoundedSemaphore(value=100)
    tflex.trainers_lock = threading.RLock()
    def add_trainer(target, delaying=True):
      #released = False
      try:
        #if delaying:
        #  time.sleep(random.random() * 60)
        with tflex.trainers_lock:
          for existing in tflex.pending_trainers:
            if existing == target:
              return
          for existing in tflex.trainers:
            if existing.target == target and tflex.trainer_alive(existing):
              return
          tflex.pending_trainers.append(target)
        try:
          with tflex.trainers_sema:
            sampler = data_sampler
            if not tflex.use_global_data_sampler:
              sampler = make_sampler(dataset=args.dataset, enc=enc, seed=seed, combine=args.combine)
            trainer = TrainGPT2(args=args, hparams=hparams, sampler=sampler, enc=enc, target=target, counter=traincounter)
          tflex.pinned_trainers.append(trainer)
          #if tflex.release_trainer_sema:
          #  tflex.trainers_sema.release()
          #  released = True
          if tflex.ensure_on_init:
            with tflex.trainers_init_sema:
              trainer.ensure()
          trainer.start()
          with tflex.trainers_lock:
            for existing in tflex.trainers:
              if existing.target == target:
                existing.stopped = True
                break
            if len(tflex.trainers) <= 0:
              print('Trainer %s is no longer fresh (first trainer)' % trainer.target)
              trainer.fresh = False
            tflex.trainers.append(trainer)
        finally:
          tflex.pending_trainers.remove(target)
      finally:
        pass
        #if not released:
        #  tflex.trainers_sema.release()
    #start_time = time.time()
    #init_timeout = 10
    #for thread in tqdm.tqdm(parallelize(targets, add_trainer)):
    #  elapsed = (time.time() - start_time)
    #  waiting = init_timeout - elapsed
    #  if waiting > 0:
    #    thread.join(timeout=waiting)
    def add_trainers(targets=None):
      if targets is None:
        targets = tflex.targets
      for thread in tqdm.tqdm(parallelize(targets, add_trainer)):
        thread.join()
    tflex.adding_trainers = False
    def add_trainers_toplevel():
      while True:
        print('Re-adding all targets...')
        add_trainers()
        time.sleep(1.0)
        while not tflex.adding_trainers:
          time.sleep(1.0)
    tflex.add_swarm_thread = threading.Thread(target=add_trainers_toplevel)
    tflex.add_swarm_thread.start()
    #maxconnections = 2
    #tflex.trainers_sema = threading.BoundedSemaphore(value=maxconnections)
    #tflex.trainers[0].fresh = False

    def get_trainers():
      for trainer in tflex.trainers:
        if tflex.trainer_alive(trainer):
          if not tflex.trainer_starting(trainer):
            yield trainer

    tflex.get_trainers = get_trainers

    @tflex.register_command
    def save():
        maketree(os.path.join(CHECKPOINT_DIR, args.run_name))
        save_trainers(tflex.get_trainers())

    #print("Warming up...")
    #def warmup(trainer):
    #  while trainer.current_step.value < 50:
    #    trainer.fit()
    #for thread in tqdm.tqdm(parallelize(tflex.get_trainers(), warmup)):
    #  thread.join()
    tflex.all_trainers = list(tflex.get_trainers())
    if args.fresh_model and len(tflex.all_trainers) > 1:
      print("Syncing...")
      tflex.update_trainers(tflex.all_trainers, 0, sync_all=True)
    print("Starting...")
    for trainer in tflex.get_trainers():
      print('Trainer %s is no longer fresh (startup trainers)' % trainer.target)
      trainer.fresh = False
      #trainer.start()
    i = 0
    sync_thread = None
    first = True
    tflex.averaging_yield_time = 1.0 # was 3.0
    tflex.averaging = True
    tflex.cycle = None
    while True:
      tflex.check_commands()
      if tflex.should_quit():
        break
      tflex.all_trainers = list(tflex.get_trainers())
      threads = []
      #for trainer in tflex.all_trainers:
      #  def thunk(trainer, n):
      #    for _ in range(n):
      #      trainer.fit()
      #  count = 1 if first else 10
      #  thread = threading.Thread(target=thunk, args=(trainer,count))
      #  thread.start()
      #  threads.append(thread)
      #for thread in threads:
      #  thread.join()
      #print('Synchronizing...', i)
      #threads = []
      if len(tflex.all_trainers) <= 0:
        time.sleep(1.0)
      else:
        i += 1
        if not tflex.averaging:
          time.sleep(1.0)
        else:
          tflex.fresh_trainers = tflex.all_trainers[:]
          if tflex.cycle is None or tflex.shuffle_cycles:
            batches = len(tflex.all_trainers[0].fetch_vars)
            tflex.cycle = list(range(batches))
            random.shuffle(tflex.cycle)
          for index in tqdm.tqdm(tflex.cycle):
            tflex.check_commands()
            if tflex.should_quit():
              break
            tflex.all_trainers = list(tflex.get_trainers())
            tflex.fresh_trainers = [x for x in tflex.fresh_trainers if x in tflex.all_trainers]
            tflex.update_trainers(tflex.all_trainers, index)
            time.sleep(tflex.averaging_yield_time) # yield some CPU and network bandwidth
          for trainer in tflex.fresh_trainers:
            print('Trainer %s is no longer fresh' % trainer.target)
            trainer.fresh = False
          first = False
          print('All done', i)


if __name__ == '__main__':
    main()

