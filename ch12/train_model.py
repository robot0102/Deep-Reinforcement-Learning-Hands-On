#!/usr/bin/env python3
import os
import sys
import random
import argparse
import logging
import numpy as np
from tensorboardX import SummaryWriter

from libbots import subtitles, data, model, cornell

import torch
import torch.nn as nn
import torch.nn.utils as nn_utils
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable


DEFAULT_FILE = "data/OpenSubtitles/en/Crime/1994/60_101020_138057_pulp_fiction.xml.gz"
DATA_DIR = "data/OpenSubtitles/en/"
SAVES_DIR = "saves"

BATCH_SIZE = 32
LEARNING_RATE = 1e-4
MAX_EPOCHES = 1000
MAX_TOKENS = 10
GRAD_CLIP = 0.1

log = logging.getLogger("train")


def load_data(args):
    if args.cornell is not None:
        dialogues = cornell.load_dialogues(genre_filter=args.cornell)
    else:
        if args.data.endswith(".xml.gz"):
            dialogues = subtitles.read_file(args.data)
        elif len(args.data) == 0:
            dialogues = subtitles.read_dir(DATA_DIR)
        else:
            data_path = os.path.join(DATA_DIR, args.data)
            dialogues = subtitles.read_dir(data_path)
    if not dialogues:
        log.error("No dialogues found, exit!")
        sys.exit()
    log.info("Loaded %d dialogues with %d phrases, generating training pairs",
             len(dialogues), sum(map(len, dialogues)))
    phrase_pairs = subtitles.dialogues_to_pairs(dialogues, max_tokens=MAX_TOKENS)
    return phrase_pairs


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)-15s %(levelname)s %(message)s", level=logging.INFO)
    parser = argparse.ArgumentParser()
    parser.add_argument("--cornell", help="Use Cornell Movie Dialogues database could be a category or empty string to load full data")
    parser.add_argument("--data", default=DEFAULT_FILE, help="Could be file name to load or category dir")
    parser.add_argument("--cuda", action='store_true', default=False, help="Enable cuda")
    parser.add_argument("-n", "--name", required=True, help="Name of the run")
    args = parser.parse_args()

    saves_path = os.path.join(SAVES_DIR, args.name)
    os.makedirs(saves_path, exist_ok=True)

    phrase_pairs = load_data(args)

    phrase_pairs_dict = subtitles.phrase_pairs_dict(phrase_pairs)
    log.info("Obtained %d phrase pairs with %d uniq words", len(phrase_pairs), len(phrase_pairs_dict))
    emb_dict, emb = data.read_embeddings(phrase_pairs_dict)
    train_data = data.encode_phrase_pairs(phrase_pairs, emb_dict)
    log.info("Sample phrases:")
    for idx, phrases, encoded in zip(range(10), phrase_pairs, train_data):
        log.info("%d: %s -> %s", idx, phrases[0].words, phrases[1].words)
        log.info("%d: %s -> %s", idx, encoded[0], encoded[1])
    log.info("Training data converted, got %d samples", len(train_data))

    data.save_embeddings(saves_path, emb_dict, emb)

    # initialize embedding lookup table
    embeddings = nn.Embedding(num_embeddings=emb.shape[0], embedding_dim=emb.shape[1])
    embeddings.weight.data.copy_(torch.from_numpy(emb))
    embeddings.weight.requires_grad = False
    if args.cuda:
        embeddings.cuda()

    net = model.PhraseModel(emb_size=emb.shape[1], dict_size=emb.shape[0], hid_size=model.HIDDEN_STATE_SIZE)
    if args.cuda:
        net.cuda()
    log.info("Model: %s", net)

    writer = SummaryWriter(comment="-" + args.name)
    optimiser = optim.Adam(net.parameters(), lr=LEARNING_RATE)
    best_bleu = None

    for epoch in range(MAX_EPOCHES):
        random.shuffle(train_data)
        losses = []
        bleu_sum = 0.0
        bleu_count = 0
        for batch in data.iterate_batches(train_data, BATCH_SIZE):
            input_seq, out_seq_list, out_idx = model.pack_batch(batch, embeddings, cuda=args.cuda)
            enc = net.encode(input_seq)

            net_results = []
            net_targets = []
            for idx, out_seq in enumerate(out_seq_list):
                r = net.decode_teacher(net.get_encoded_item(enc, idx), out_seq)
                net_results.append(r)
                net_targets.extend(out_idx[idx][1:])
                bleu_sum += model.seq_bleu(r, out_idx[idx][1:])
                bleu_count += 1
            results_v = torch.cat(net_results)
            targets_v = Variable(torch.LongTensor(net_targets))
            if args.cuda:
                targets_v = targets_v.cuda()
            loss_v = F.cross_entropy(results_v, targets_v)
            loss_v.backward()
            nn_utils.clip_grad_norm(net.parameters(), GRAD_CLIP)
            optimiser.step()

            losses.append(loss_v.data.cpu().numpy()[0])
        bleu = bleu_sum / bleu_count
        log.info("Epoch %d: mean loss %.3f, mean BLEU %.3f", epoch, np.mean(losses), bleu)
        writer.add_scalar("loss", np.mean(losses), epoch)
        writer.add_scalar("bleu", bleu, epoch)
        if best_bleu is None or best_bleu < bleu:
            if best_bleu is not None:
                torch.save(net.state_dict(), os.path.join(saves_path, "pre_bleu_%.3f_%02d.dat" % (bleu, epoch)))
                log.info("Best BLEU updated %.3f", bleu)
            best_bleu = bleu
    writer.close()