# Copyright 2022 InstaDeep Ltd. All rights reserved.
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

import functools
import logging
from typing import Dict, Tuple

import hydra
import jax
import jax.numpy as jnp
import omegaconf
from tqdm.auto import trange

from jumanji.training import utils
from jumanji.training.agents.random import RandomAgent
from jumanji.training.loggers import TerminalLogger
from jumanji.training.setup_train import (
    setup_agent,
    setup_env,
    setup_evaluators,
    setup_logger,
    setup_training_state,
)
from jumanji.training.timer import Timer
from jumanji.training.types import TrainingState

from a2c import A2CAgent
from jumanji.training.setup_train import _setup_actor_critic_neworks
import optax



def train(cfg: omegaconf.DictConfig, log_compiles: bool = False, gpu_acting: bool = False) -> None:
    print(f"gpu acting {gpu_acting}")

    logging.info(omegaconf.OmegaConf.to_yaml(cfg))
    logging.getLogger().setLevel(logging.INFO)
    logging.info({"devices": jax.local_devices()})

    key, init_key = jax.random.split(jax.random.PRNGKey(cfg.seed))
    logger = setup_logger(cfg)
    env = setup_env(cfg)
    actor_critic_networks = _setup_actor_critic_neworks(cfg, env)
    optimizer = optax.adam(cfg.env.a2c.learning_rate)
    agent = A2CAgent(
        env=env,
        n_steps=cfg.env.training.n_steps,
        total_batch_size=cfg.env.training.total_batch_size,
        actor_critic_networks=actor_critic_networks,
        optimizer=optimizer,
        normalize_advantage=cfg.env.a2c.normalize_advantage,
        discount_factor=cfg.env.a2c.discount_factor,
        bootstrapping_factor=cfg.env.a2c.bootstrapping_factor,
        l_pg=cfg.env.a2c.l_pg,
        l_td=cfg.env.a2c.l_td,
        l_en=cfg.env.a2c.l_en,
        gpu_acting=gpu_acting
    )
    stochastic_eval, greedy_eval = setup_evaluators(cfg, agent)
    training_state = setup_training_state(env, agent, init_key)
    num_steps_per_epoch = (
        cfg.env.training.n_steps
        * cfg.env.training.total_batch_size
        * cfg.env.training.num_learner_steps_per_epoch
    )
    eval_timer = Timer(out_var_name="metrics")
    train_timer = Timer(
        out_var_name="metrics", num_steps_per_timing=num_steps_per_epoch
    )


    def epoch_fn(training_state: TrainingState) -> Tuple[TrainingState, Dict]:
        training_state = jax.tree_map(lambda x: x[0], training_state)

        if not gpu_acting:
            policy_params, acting_state = jax.device_put((training_state.params_state.params.actor,
                                                          training_state.acting_state),
                                                         device=jax.devices("cpu")[0])
        else:
            policy_params, acting_state = (training_state.params_state.params.actor,
                                                          training_state.acting_state)

        acting_state, data = agent.rollout(
            policy_params=policy_params,
            acting_state=acting_state,
        )  # data.shape == (T, B, ...)

        if not gpu_acting:
            acting_state, data = jax.device_put((acting_state, data), device=jax.devices()[0])

        training_state = training_state._replace(acting_state=acting_state)
        training_state, metrics = jax.jit(agent.gradient_step)(training_state, data)
        metrics = jax.tree_util.tree_map(jnp.mean, metrics)

        training_state = jax.tree_map(lambda x: x[None], training_state)
        return training_state, metrics

    with jax.log_compiles(log_compiles), logger:
        for i in trange(
            cfg.env.training.num_epochs,
            disable=isinstance(logger, TerminalLogger),
        ):
            env_steps = i * num_steps_per_epoch

            # Evaluation
            key, stochastic_eval_key, greedy_eval_key = jax.random.split(key, 3)
            # Stochastic evaluation
            with eval_timer:
                metrics = stochastic_eval.run_evaluation(
                    training_state.params_state, stochastic_eval_key
                )
                jax.block_until_ready(metrics)
            logger.write(
                data=utils.first_from_device(metrics),
                label="eval_stochastic",
                env_steps=env_steps,
            )
            if not isinstance(agent, RandomAgent):
                # Greedy evaluation
                with eval_timer:
                    metrics = greedy_eval.run_evaluation(
                        training_state.params_state, greedy_eval_key
                    )
                    jax.block_until_ready(metrics)
                logger.write(
                    data=utils.first_from_device(metrics),
                    label="eval_greedy",
                    env_steps=env_steps,
                )

            # Training
            with train_timer:
                training_state, metrics = epoch_fn(training_state)
                jax.block_until_ready((training_state, metrics))
            logger.write(
                data=metrics,
                label="train",
                env_steps=env_steps,
            )