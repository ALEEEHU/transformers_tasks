# !/usr/bin/env python3
"""
==== No Bugs in code, just some Random Unexpected FEATURES ====
┌─────────────────────────────────────────────────────────────┐
│┌───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┬───┐│
││Esc│!1 │@2 │#3 │$4 │%5 │^6 │&7 │*8 │(9 │)0 │_- │+= │|\ │`~ ││
│├───┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴───┤│
││ Tab │ Q │ W │ E │ R │ T │ Y │ U │ I │ O │ P │{[ │}] │ BS  ││
│├─────┴┬──┴┬──┴┬──┴┬──┴┬──┴┬──┴┬──┴┬──┴┬──┴┬──┴┬──┴┬──┴─────┤│
││ Ctrl │ A │ S │ D │ F │ G │ H │ J │ K │ L │: ;│" '│ Enter  ││
│├──────┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴─┬─┴────┬───┤│
││ Shift  │ Z │ X │ C │ V │ B │ N │ M │< ,│> .│? /│Shift │Fn ││
│└─────┬──┴┬──┴──┬┴───┴───┴───┴───┴───┴──┬┴───┴┬──┴┬─────┴───┘│
│      │Fn │ Alt │         Space         │ Alt │Win│   HHKB   │
│      └───┴─────┴───────────────────────┴─────┴───┘          │
└─────────────────────────────────────────────────────────────┘

PPO + GPT2, 中文情感分析。

Author: pankeyu
Date: 2022/12/27
"""
import time
import random

import torch
from rich import print
from tqdm import tqdm
import numpy as np

from transformers import AutoTokenizer, AutoModelForSequenceClassification, pipeline

from trl.gpt2 import GPT2HeadWithValueModel
from trl.ppo import PPOTrainer

from iTrainingLogger import iSummaryWriter


writer = iSummaryWriter(log_path='./logs', log_name='PPO-Sentiment-Zh')
config = {
    "model_name": "uer/gpt2-chinese-cluecorpussmall",
    "steps": 20000,
    "batch_size": 128,
    "forward_batch_size": 16,
    "ppo_epochs": 4,   
    "lr": 1.41e-5,
    "init_kl_coef":0.2,
    "target": 6,
    "horizon":10000,
    "gamma":1,
    "lam":0.95,
    "cliprange": .2,
    "cliprange_value":.2,
    "vf_coef":.1,
    "gen_len": 16
}

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
pipe_device = 0 if torch.cuda.is_available() else -1

# prompt池
prompts = [
    '刚收到货，感觉',
    '这部电影很',
    '说实话，真的很',
    '这次购物总的来说体验很'
]

# 情感分类模型——「情绪识别模型」我们选用 transformers 中内置的 sentiment-analysis pipeline 来实现。
senti_tokenizer = AutoTokenizer.from_pretrained('uer/roberta-base-finetuned-jd-binary-chinese')
senti_model = AutoModelForSequenceClassification.from_pretrained('uer/roberta-base-finetuned-jd-binary-chinese')
sentiment_pipe = pipeline('sentiment-analysis', model=senti_model, tokenizer=senti_tokenizer, device=pipe_device)

# 文本生成模型
gpt2_model = GPT2HeadWithValueModel.from_pretrained(config['model_name'])
gpt2_model_ref = GPT2HeadWithValueModel.from_pretrained(config['model_name'])
gpt2_tokenizer = AutoTokenizer.from_pretrained(config['model_name'])
gpt2_tokenizer.eos_token = gpt2_tokenizer.pad_token
gpt2_model.to(device)
gpt2_model_ref.to(device)

gen_kwargs = {
    "min_length":-1,
    "top_k": 0.0,
    "top_p": 1.0,
    "do_sample": True,
    "pad_token_id": gpt2_tokenizer.eos_token_id
}

# RL Trainer
ppo_trainer = PPOTrainer(gpt2_model, gpt2_model_ref, gpt2_tokenizer, **config) # initialize the agent
total_ppo_epochs = int(np.ceil(config["steps"]/config['batch_size']))

for epoch in tqdm(range(total_ppo_epochs)):#total_ppo_epochs=157
    logs, timing = dict(), dict()
    t0 = time.time()

    batch = {
        'tokens': [],
        'query': []
    }
    for _ in range(config['batch_size']):
        random_prompt = random.choice(prompts)                                  # 随机选择一个prompt
        tokens = gpt2_tokenizer.encode(random_prompt)
        batch['tokens'].append(tokens)
        batch['query'].append(random_prompt)
    query_tensors = [torch.tensor(t).long().to(device) for t in batch["tokens"]] #len(batch['tokens'])=128

    t = time.time()
    response_tensors = []
    for i in range(config['batch_size']):#config['batch_size']=128
        gen_len = config['gen_len']#16
        response = gpt2_model.generate(query_tensors[i].unsqueeze(dim=0),
                                       max_new_tokens=gen_len, **gen_kwargs)
        response_tensors.append(response.squeeze()[-gen_len:])
    batch['response'] = [gpt2_tokenizer.decode(r.squeeze()) for r in response_tensors]#len=128
    timing['time/get_response'] = time.time() - t

    t = time.time()
    texts = [q + r for q,r in zip(batch['query'], batch['response'])]           # 计算正向/负向情感得分
    pipe_outputs = sentiment_pipe(texts)#128
    rewards = []
    for output in pipe_outputs:
        if output['label'] == 'positive (stars 4 and 5)':
            rewards.append(output['score'])
        elif output['label'] == 'negative (stars 1, 2 and 3)':
            rewards.append(1 - output['score'])
        else:
            raise ValueError(f"错误的推理结果{output['label']}.")
    rewards = torch.tensor(rewards).to(device)                                  # 将正向情感的得分作为生成得分,score值作为reward，会输入agent中进行优化
    timing['time/get_sentiment_preds'] = time.time() - t

    t = time.time()
    stats = ppo_trainer.step(query_tensors, response_tensors, rewards)          # PPO Update 
    # show logs and curves
    timing['time/optimization'] = time.time() - t

    timing['time/epoch'] = time.time() - t0                                     # logging
    logs.update(timing)
    logs.update(stats)
    logs['env/reward_mean'] = torch.mean(rewards).cpu().numpy()
    logs['env/reward_std'] = torch.std(rewards).cpu().numpy()
    logs['env/reward_dist'] = rewards.cpu().numpy()
    print(f"epoch {epoch} mean-reward: {logs['env/reward_mean']}")

    print('Random Sample 5 text(s) of model output:')
    for i in range(5):                                                           # 随机打5个生成的结果
        print(f'{i+1}. {random.choice(texts)}')

    writer.add_scalar('train/reward', logs['env/reward_mean'], epoch)
    for k, v in timing.items():
        writer.add_scalar(k, v, epoch)
    writer.add_scalar('ppo/loss/policy', stats['ppo/loss/policy'], epoch)
    writer.add_scalar('ppo/loss/value', stats['ppo/loss/value'], epoch)
    writer.add_scalar('ppo/policy/entropy', stats['ppo/policy/entropy'], epoch)
    writer.add_scalar('ppo/policy/policykl', stats['ppo/policy/policykl'], epoch)
    writer.record()