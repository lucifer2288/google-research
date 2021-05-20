# coding=utf-8
# Copyright 2021 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Launch script for training."""

import logging
import os
import os.path as osp
import random

from absl import app
from absl import flags
from ml_collections import config_flags
from ml_collections import ConfigDict
import numpy as np
import torch
from torchkit import checkpoint
from torchkit import Logger
from torchkit.utils.py_utils import Stopwatch
from xirl import common
import yaml

FLAGS = flags.FLAGS

flags.DEFINE_string("experiment_name", None, "Experiment name.")
flags.DEFINE_boolean("resume", False, "Whether to resume training.")

config_flags.DEFINE_config_file(
    "config",
    "configs/pretraining.py",
    "File path to the training hyperparameter configuration.",
    lock_config=True,
)

flags.mark_flag_as_required("experiment_name")


def seed_rng(seed):
  """Seeds python, numpy, and torch RNGs."""
  random.seed(seed)
  np.random.seed(seed)
  torch.manual_seed(seed)
  torch.cuda.manual_seed(seed)
  torch.backends.cudnn.deterministic = FLAGS.config.CUDNN_DETERMINISTIC
  torch.backends.cudnn.benchmark = FLAGS.config.CUDNN_BENCHMARK


def setup_experiment(exp_dir):
  """Initializes a training experiment."""
  if os.path.exists(exp_dir):
    if not FLAGS.resume:
      raise ValueError(
          "Experiment already exists. Run with --resume to continue.")
    with open(os.path.join(exp_dir, "config.yaml"), "r") as fp:
      cfg = yaml.load(fp, Loader=yaml.FullLoader)
    FLAGS.config.update(cfg)
  else:
    os.makedirs(exp_dir)
    with open(os.path.join(exp_dir, "config.yaml"), "w") as fp:
      yaml.dump(ConfigDict.to_dict(FLAGS.config), fp)


def main(_):
  exp_dir = osp.join(FLAGS.config.ROOT_DIR, FLAGS.experiment_name)
  setup_experiment(exp_dir)

  # Set RNG seeds.
  if FLAGS.config.SEED is not None:
    logging.info(f"Experiment seed: {FLAGS.config.SEED}.")  # pylint: disable=logging-format-interpolation
    seed_rng(FLAGS.config.SEED)
  else:
    logging.info("No RNG seed has been set for this experiment.")

  # Setup compute device.
  if torch.cuda.is_available():
    device = torch.device("cuda")
    logging.info(f"Using GPU {torch.cuda.get_device_name(device)}.")  # pylint: disable=logging-format-interpolation
  else:
    logging.info("No GPU found. Falling back to CPU.")
    device = torch.device("cpu")

  logger = Logger(exp_dir, FLAGS.resume)

  # Load factories.
  (
      model,
      optimizer,
      pretrain_loaders,
      downstream_loaders,
      trainer,
      eval_manager,
  ) = common.get_factories(FLAGS.config, device)

  # Create checkpoint manager.
  checkpoint_dir = osp.join(exp_dir, "checkpoints")
  checkpoint_manager = checkpoint.CheckpointManager(
      checkpoint.Checkpoint(model=model, optimizer=optimizer),
      checkpoint_dir,
      device,
  )

  global_step = checkpoint_manager.restore_or_initialize()
  total_batches = max(1, len(pretrain_loaders["train"]))
  epoch = int(global_step / total_batches)
  complete = False
  stopwatch = Stopwatch()
  try:
    while not complete:
      logger.log_learning_rate(optimizer, global_step, "pretrain")
      for batch in pretrain_loaders["train"]:
        train_loss = trainer.train_one_iter(batch)

        if not global_step % FLAGS.config.LOGGING_FREQUENCY:
          for k, v in train_loss.items():
            logger.log_scalar(v, global_step, k, "pretrain")

        if not global_step % FLAGS.config.EVAL.EVAL_FREQUENCY:
          # Evaluate the model on the pretraining validation dataset.
          valid_loss = trainer.eval_num_iters(pretrain_loaders["valid"],
                                              FLAGS.config.EVAL.VAL_ITERS)
          for k, v in valid_loss.items():
            logger.log_scalar(v, global_step, k, "pretrain")

          # Evaluate the model on the downstream datasets.
          for split, downstream_loader in downstream_loaders.items():
            eval_to_metric = eval_manager.evaluate(
                model,
                downstream_loader,
                device,
                FLAGS.config.EVAL.VAL_ITERS,
            )
            for eval_name, eval_out in eval_to_metric.items():
              eval_out.log(
                  logger,
                  global_step,
                  eval_name,
                  f"downstream/{split}",
              )

        # Save model checkpoint.
        if not global_step % FLAGS.config.CHECKPOINTING_FREQUENCY:
          checkpoint_manager.save(global_step)

        # Exit if complete.
        global_step += 1
        if global_step > FLAGS.config.OPTIM.TRAIN_MAX_ITERS:
          complete = True
          break

        time_per_iter = stopwatch.elapsed()
        logging.info(
            "Iter[{}/{}] (Epoch {}), {:.1f}s/iter, Loss: {:.3f}".format(
                global_step,
                FLAGS.config.OPTIM.TRAIN_MAX_ITERS,
                epoch,
                time_per_iter,
                train_loss["train/total_loss"].item(),
            ))
        stopwatch.reset()
      epoch += 1

  except KeyboardInterrupt:
    logging.info("Caught keyboard interrupt. Saving model before quitting.")

  finally:
    checkpoint_manager.save(global_step)
    logger.close()


if __name__ == "__main__":
  app.run(main)
