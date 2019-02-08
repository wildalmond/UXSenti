import argparse
import os
import time
import math
import hashlib
import numpy as np
import pickle
import torch
import torch.nn as nn
import torch.nn.functional as F
from fastai.text.models.awd_lstm import *
from utils.vocab import *
from utils.data import *
from utils.utils import *
from utils.module import *
from model import get_cross_lingual_language_model
from trainer import CrossLingualLanguageModelTrainer
from discriminator import Discriminator

TEST_SENTS = [
    'i love',
    'this book is',
    'i likes the',
    'do you feel different from people',
]
PRED_NWORDS = 20


def model_save(trainer, path):
    trainer.rev_grad = None

    with open(path, 'wb') as f:
        torch.save(trainer, f)

    trainer.rev_grad = GradReverse(trainer.lambd)


def model_load(path):
    with open(path, 'rb') as f:
        trainer = torch.load(f)

    trainer.rev_grad = GradReverse(trainer.lambd)

    return trainer


def sample(sents, model, n_words, vocab):
    def pred_ids(x):
        res = []
        x = x.view(1,  -1)
        for i in range(n_words):
            output, _, _ = model(x)
            output = output.view(-1, output.size(-1))
            _, pred = output[-1].max(-1)
            res.append(pred.item())
            x = pred.view(1, 1)
        return res

    model.eval()
    model.reset()
    pred_sents = []
    for sent in TEST_SENTS:
        word_ids = torch.tensor([vocab.w2idx[w] for w in sent.split()])
        if next(model.parameters()).is_cuda:
            word_ids = word_ids.cuda()
        pred = pred_ids(word_ids)
        pred_sents.append(' '.join(sent.split() + [vocab.idx2w[i] for i in pred]))

    return pred_sents


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('-src', '--src', default='en', help='source_language')
    parser.add_argument('-trg', '--trg', default='fr', help='target_language')
    parser.add_argument('--lexicon', default='data/muse/en-fr.0-5000.txt', help='lexicon file')
    parser.add_argument('--src_vocab', default='data/vocab_en.txt', help='src vocab file')
    parser.add_argument('--trg_vocab', default='data/vocab_fr.txt', help='trg vocab file')

    parser.add_argument('--model', type=str, default='LSTM', help='type of recurrent net (LSTM, QRNN, GRU)')
    parser.add_argument('--emsize', type=int, default=400, help='size of word embeddings')
    parser.add_argument('--nhid', type=int, default=1150, help='number of hidden units per layer')
    parser.add_argument('--dis_nhid', type=int, default=1024, help='number of hidden units per layer')
    parser.add_argument('--nlayers', type=int, default=3, help='number of layers')
    parser.add_argument('--dis_nlayers', type=int, default=2, help='number of layers')

    parser.add_argument('--epochs', type=int, default=8000000, help='upper epoch limit')
    parser.add_argument('--tied', type=bool_flag, nargs='?', const=True, default=True, help='tied embeddings')
    parser.add_argument('--batch_size', type=int, default=80, metavar='N', help='batch size')
    parser.add_argument('--bptt', type=int, default=70, help='sequence length')
    parser.add_argument('--dropout', type=float, default=0.4, help='dropout applied to layers (0 = no dropout)')
    parser.add_argument('--dropouth', type=float, default=0.3, help='dropout for rnn layers (0 = no dropout)')
    parser.add_argument('--dropouti', type=float, default=0.65, help='dropout for input embedding layers (0 = no dropout)')
    parser.add_argument('--dropoute', type=float, default=0.1, help='dropout to remove words from embedding layer (0 = no dropout)')
    parser.add_argument('--wdrop', type=float, default=0.5, help='amount of weight dropout to apply to the RNN hidden to hidden matrix')
    parser.add_argument('--seed', type=int, default=1111, help='random seed')
    parser.add_argument('--cuda', type=bool_flag, nargs='?', const=True, default=True, help='use CUDA')
    parser.add_argument('--log_interval', type=int, default=200, metavar='N', help='report interval')
    parser.add_argument('--val_interval', type=int, default=1000, metavar='N', help='validation interval')
    randomhash = str(time.time()).split('.')[0]
    parser.add_argument('--export', type=str,  default='export/', help='dir to save the model')

    parser.add_argument('--lm_lr', type=float, default=0.004, help='initial learning rate')
    parser.add_argument('--dis_lr', type=float, default=0.0003, help='initial learning rate')
    parser.add_argument('--lm_clip', type=float, default=0.25, help='gradient clipping')
    parser.add_argument('--dis_clip', type=float, default=0.1, help='gradient clipping')
    parser.add_argument('--alpha', type=float, default=2, help='alpha L2 regularization on RNN activation (alpha = 0 means no regularization)')
    parser.add_argument('--beta', type=float, default=1, help='beta slowness regularization applied on RNN activiation (beta = 0 means no regularization)')
    parser.add_argument('--lambd', type=float, default=1, help='beta slowness regularization applied on RNN activiation (beta = 0 means no regularization)')
    parser.add_argument('--optimizer', type=str,  default='adam', help='optimizer to use (sgd, adam)')
    parser.add_argument('--adam_beta', type=float, default=0.7, help='beta of adam')

    parser.add_argument('--wdecay', type=float, default=1.2e-6, help='weight decay applied to all weights')
    parser.add_argument('--resume', type=str,  default='', help='path of model to resume')
    parser.add_argument('--when', nargs="+", type=int, default=[-1], help='When (which epochs) to divide the learning rate by 10 - accepts multiple')
    args = parser.parse_args()

    sl = 'ja' if args.src == 'jp' else args.src
    tl = 'ja' if args.trg == 'jp' else args.trg
    parser.set_defaults(lexicon=os.path.join('data', 'muse', '{}-{}.0-5000.txt'.format(sl, tl)),
                        src_vocab=os.path.join('data', 'vocab_{}.txt'.format(args.src)),
                        trg_vocab=os.path.join('data', 'vocab_{}.txt'.format(args.trg)))

    args = parser.parse_args()

    # Set the random seed manually for reproducibility.
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if torch.cuda.is_available():
        if not args.cuda:
            print("WARNING: You have a CUDA device, so you should probably run with --cuda")
        else:
            torch.cuda.manual_seed(args.seed)

    print('Configuration:')
    print('\n'.join('\t{:15} {}'.format(k + ':', str(v)) for k, v in sorted(dict(vars(args)).items())))
    print()

    model_path = os.path.join(args.export, 'model.pt')
    config_path = os.path.join(args.export, 'config.json')
    export_config(args, config_path)
    check_path(model_path)

    ###############################################################################
    # Load data
    ###############################################################################
    print('Statistics:')

    # load vocabulary
    src_vocab = Vocab(path=args.src_vocab)
    trg_vocab = Vocab(path=args.trg_vocab)
    print('\tsrc vocab size: {}'.format(len(src_vocab)))
    print('\ttrg vocab size: {}'.format(len(trg_vocab)))

    lexicon, lex_sz = load_lexicon(args.lexicon, src_vocab, trg_vocab)
    print('\tlexicon size:   {}'.format(len(lexicon)))
    print('\tlex oov rate:   {}'.format(1 - len(lexicon) / lex_sz))

    with open(os.path.join('pickle', args.src, 'full.txt'), 'rb') as fin:
        src_x = pickle.load(fin)
    with open(os.path.join('pickle', args.trg, 'full.txt'), 'rb') as fin:
        trg_x = pickle.load(fin)

    if args.cuda:
        src_x = src_x.cuda()
        trg_x = trg_x.cuda()

    print('\tsrc size:       {}'.format(src_x.size(0)))
    print('\ttrg size:       {}'.format(trg_x.size(0)))
    src_size = src_x.size(0)
    trg_size = trg_x.size(0)
    src_train, src_val = src_x[:int(src_size * 0.8)], src_x[int(src_size * 0.8):]
    trg_train, trg_val = trg_x[:int(trg_size * 0.8)], trg_x[int(trg_size * 0.8):]

    print('\tsrc train size: {}'.format(src_train.size(0)))
    print('\tsrc val size :  {}'.format(src_val.size(0)))
    print('\ttrg train size: {}'.format(trg_train.size(0)))
    print('\ttrg val size :  {}'.format(trg_val.size(0)))
    print()

    src_train, src_val = batchify(src_train, args.batch_size), batchify(src_val, args.batch_size)
    trg_train, trg_val = batchify(trg_train, args.batch_size), batchify(trg_val, args.batch_size)

    ###############################################################################
    # Build the model
    ###############################################################################

    if args.resume:
        # print('Resuming model ...')
        trainer = model_load(args.resume)

    else:
        src_lm, trg_lm = get_cross_lingual_language_model(src_ntok=len(src_vocab), trg_ntok=len(trg_vocab), emb_sz=args.emsize,
                                                          n_hid=args.nhid, n_layers=args.nlayers, tie_weights=True,
                                                          output_p=args.dropout, hidden_p=args.dropouth, input_p=args.dropouti,
                                                          embed_p=args.dropoute, weight_p=args.wdrop)

        dis_in_dim = (args.nlayers - 1) * args.nhid + args.emsize if args.tied else args.nlayers * args.nhid
        discriminator = Discriminator(dis_in_dim, args.dis_nhid, 2, nlayers=args.dis_nlayers, dropout=0.1)
        criterion = nn.NLLLoss()
        params = set(src_lm.parameters()) | set(trg_lm.parameters())
        if args.optimizer == 'sgd':
            lm_optimizer = torch.optim.SGD(params, lr=args.lm_lr, weight_decay=args.wdecay)
            dis_optimizer = torch.optim.SGD(discriminator.parameters(), lr=args.dis_lr, weight_decay=args.wdecay)
        if args.optimizer == 'adam':
            lm_optimizer = torch.optim.Adam(params, lr=args.lm_lr, weight_decay=args.wdecay, betas=(args.adam_beta, 0.999))
            dis_optimizer = torch.optim.Adam(discriminator.parameters(), lr=args.dis_lr, weight_decay=args.wdecay, betas=(args.adam_beta, 0.999))

        trainer = CrossLingualLanguageModelTrainer(src_lm, trg_lm, discriminator, lm_optimizer,
                                                   dis_optimizer, criterion, args.bptt, args.alpha,
                                                   args.beta, args.lambd, args.lm_clip, args.dis_clip,
                                                   lexicon, lex_sz)

    if args.cuda:
        trainer.cuda()
    else:
        trainer.cpu()

    print('Parameters:')
    total_params = sum(x.size()[0] * x.size()[1] if len(x.size()) > 1 else x.size()[0] for x in params if x.size())
    print('\ttotal params:   {}'.format(total_params))

    print('\tparam list:     {}'.format(len(list(params))))
    for name, x in src_lm.named_parameters():
        print('\t' + name + '\t', tuple(x.size()))
    for name, x in trg_lm.named_parameters():
        print('\t' + name + '\t', tuple(x.size()))
    for name, x in discriminator.named_parameters():
        print('\t' + name + '\t', tuple(x.size()))
    print()

    ###############################################################################
    # Training code
    ###############################################################################

    # Loop over epochs.
    best_val_loss = np.array([float('inf')] * 4)
    print('Traning:')
    # At any point you can hit Ctrl + C to break out of training early.
    try:
        src_p, trg_p = 0, 0
        total_loss = np.zeros(4)
        start_time = time.time()

        for epoch in range(1, args.epochs + 1):

            # sample seq_len
            bptt = args.bptt if np.random.random() < 0.95 else args.bptt / 2.
            seq_len = max(5, int(np.random.normal(bptt, 5)))

            if src_p + seq_len > src_train.size(0):
                src_p = 0
                trainer.reset_src()
            if trg_p + seq_len > trg_train.size(0):
                trg_p = 0
                trainer.reset_trg()

            # fetch a batch of data
            sx, sy = get_batch(src_train, src_p, args.bptt, seq_len=seq_len, batch_first=True)
            tx, ty = get_batch(trg_train, trg_p, args.bptt, seq_len=seq_len, batch_first=True)

            losses = trainer.step(sx, sy, tx, ty)
            total_loss += np.array(losses)

            src_p += seq_len
            trg_p += seq_len

            if (epoch + 1) % args.log_interval == 0:
                cur_loss = total_loss / args.log_interval
                elapsed = time.time() - start_time

                print('| epoch {:4d} | lm_lr {:05.5f} | ms/batch {:5.2f} | '
                      ' loss {:5.2f} | src_ppl {:7.2f} | trg_ppl {:7.2f} | dis_loss {:5.2f} |'.format(
                          epoch, lm_optimizer.param_groups[0]['lr'], elapsed * 1000 / args.log_interval,
                          cur_loss[0], math.exp(cur_loss[1]), math.exp(cur_loss[2]), cur_loss[3]))

                total_loss = 0
                start_time = time.time()

            if (epoch + 1) % args.val_interval == 0:
                val_loss = trainer.evaluate(src_val, trg_val)
                acc = trainer.evaluate_bdi()

                print('-' * 91)
                print('| epoch {:4d} | acc {:4.2f} | loss {:5.2f} | src_ppl {:7.2f} | trg_ppl {:7.2f} | dis_loss {:5.2f} |'.format(
                    epoch, acc, val_loss[0], math.exp(val_loss[1]), math.exp(val_loss[2]), val_loss[3]))
                print('-' * 91)

                if val_loss[0] < best_val_loss[0]:
                    print('saving model to {}'.format(model_path))
                    model_save(trainer, model_path)
                    best_val_loss = val_loss

    except KeyboardInterrupt:
        print('-' * 91)
        print('Keyboard Interrupte - Exiting from training early')

    ###############################################################################
    # Testing
    ###############################################################################

    model_load(model_path)   # Load the best saved model.

    # print('Generated sentences:')
    # pred_sents = sample(TEST_SENTS, model, PRED_NWORDS, src_vocab)
    # print('\n\t'.join(pred_sents))

    # test_loss = evaluate(test_data, model, criterion, args.bptt)
    # print('-' * 91)
    # print('| End of training | test loss {:5.2f} | test ppl {:8.2f} | test bpc {:8.3f}'.format(
    #     test_loss, math.exp(test_loss), test_loss / math.log(2)))
    # print('-' * 91)


if __name__ == '__main__':
    main()