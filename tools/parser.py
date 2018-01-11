#!/usr/bin/python -*- coding: utf-8 -*-

import io
import os
import re
import sys
import copy
import string
import timeit
import random
import pickle
import socket
import threading

import argparse
import numpy as np
from collections import Counter, namedtuple, defaultdict

import dynet as dy
from gensim.models.word2vec import *

from utils.arcEager import ArcEager
from utils.pseudoProjectivity import *

random.seed(37)
np.random.seed(37)


class Meta:
    def __init__(self):
        self.c_dim = 32  # character-rnn input dimension
        self.window = 2  # arc-eager feature window
        self.add_words = 1  # additional lookup for missing/special words
        self.p_hidden = 64  # pos-mlp hidden layer dimension
        self.n_hidden = 128  # parser-mlp hidden layer dimension
        self.lstm_wc_dim = 128  # LSTM (word-char concatenated input) output dimension
        self.lstm_char_dim = 32  # char-LSTM output dimension
        self.transitions = {'SHIFT':0,'LEFTARC':1,'RIGHTARC':2,'REDUCE':3}  # parser transitions

class Configuration(object):
    def __init__(self, nodes=[]):
        self.stack = list()
        self.b0 = 1
        self.nodes = nodes

class Parser(ArcEager):
    def __init__(self, model=None, meta=None):
        self.model = dy.Model()
        self.meta = pickle.load(open('%s.meta' %model, 'rb')) if model else meta

        # define pos-mlp
        self.ps_pW1 = self.model.add_parameters((self.meta.p_hidden, self.meta.lstm_wc_dim*2))
        self.ps_pb1 = self.model.add_parameters(self.meta.p_hidden) 
        self.ps_pW2 = self.model.add_parameters((self.meta.n_tags, self.meta.p_hidden)) 
        self.ps_pb2 = self.model.add_parameters(self.meta.n_tags)

        # define parse-mlp
        self.pr_pW1 = self.model.add_parameters((self.meta.n_hidden, self.meta.lstm_wc_dim*2*self.meta.window))
        self.pr_pb1 = self.model.add_parameters(self.meta.n_hidden) 
        self.pr_pW2 = self.model.add_parameters((self.meta.n_outs, self.meta.n_hidden)) 
        self.pr_pb2 = self.model.add_parameters(self.meta.n_outs)

        # define char-rnns
        self.cfwdRNN = dy.LSTMBuilder(1, self.meta.c_dim, self.meta.lstm_char_dim, self.model)
        self.cbwdRNN = dy.LSTMBuilder(1, self.meta.c_dim, self.meta.lstm_char_dim, self.model)

        # define base Bi-LSTM for input word sequence (takes word+char-rnn embeddings as input)
        self.fwdRNN = dy.LSTMBuilder(1, self.meta.w_dim+self.meta.lstm_char_dim*2, self.meta.lstm_wc_dim, self.model)
        self.bwdRNN = dy.LSTMBuilder(1, self.meta.w_dim+self.meta.lstm_char_dim*2, self.meta.lstm_wc_dim, self.model)

        # define Bi-LSTM for POS feature representation (takes base Bi-LSTM output as input)
        self.ps_fwdRNN = dy.LSTMBuilder(1, self.meta.lstm_wc_dim*2, self.meta.lstm_wc_dim, self.model)
        self.ps_bwdRNN = dy.LSTMBuilder(1, self.meta.lstm_wc_dim*2, self.meta.lstm_wc_dim, self.model)

        # define Bi-LSTM for parser feature representation (takes base Bi-LSTM output and pos-hidden-state as input)
        self.pr_fwdRNN = dy.LSTMBuilder(1, self.meta.lstm_wc_dim*2+self.meta.p_hidden, self.meta.lstm_wc_dim, self.model)
        self.pr_bwdRNN = dy.LSTMBuilder(1, self.meta.lstm_wc_dim*2+self.meta.p_hidden, self.meta.lstm_wc_dim, self.model)

        # pad-node for missing nodes in partial parse tree
        self.PAD = self.model.add_parameters(self.meta.lstm_wc_dim*2)

        # define lookup tables
        self.LOOKUP_WORD = self.model.add_lookup_parameters((self.meta.n_words+self.meta.add_words, self.meta.w_dim))
        self.LOOKUP_CHAR = self.model.add_lookup_parameters((self.meta.n_chars, self.meta.c_dim))

        # load pretrained embeddings
        if model is None:
            for word, V in wvm.vocab.iteritems():
                self.LOOKUP_WORD.init_row(V.index+self.meta.add_words, wvm.syn0[V.index])

        # load pretrained dynet model
        if model:
            self.model.populate('%s.dy' %model)

    def enable_dropout(self):
        self.fwdRNN.set_dropout(0.3)
        self.bwdRNN.set_dropout(0.3)
        self.cfwdRNN.set_dropout(0.3)
        self.cbwdRNN.set_dropout(0.3)
        self.ps_fwdRNN.set_dropout(0.3)
        self.ps_bwdRNN.set_dropout(0.3)
        self.pr_fwdRNN.set_dropout(0.3)
        self.pr_bwdRNN.set_dropout(0.3)
        self.ps_W1 = dy.dropout(self.ps_W1, 0.3)
        self.ps_b1 = dy.dropout(self.ps_b1, 0.3)
        self.pr_W1 = dy.dropout(self.pr_W1, 0.3)
        self.pr_b1 = dy.dropout(self.pr_b1, 0.3)

    def disable_dropout(self):
        self.fwdRNN.disable_dropout()
        self.bwdRNN.disable_dropout()
        self.cfwdRNN.disable_dropout()
        self.cbwdRNN.disable_dropout()
        self.ps_fwdRNN.disable_dropout()
        self.ps_bwdRNN.disable_dropout()
        self.pr_fwdRNN.disable_dropout()
        self.pr_bwdRNN.disable_dropout()

    def initialize_graph_nodes(self):
        #  convert parameters to expressions
        self.pad = dy.parameter(self.PAD)

        self.ps_W1 = dy.parameter(self.ps_pW1)
        self.ps_b1 = dy.parameter(self.ps_pb1)
        self.ps_W2 = dy.parameter(self.ps_pW2)
        self.ps_b2 = dy.parameter(self.ps_pb2)
        self.pr_W1 = dy.parameter(self.pr_pW1)
        self.pr_b1 = dy.parameter(self.pr_pb1)
        self.pr_W2 = dy.parameter(self.pr_pW2)
        self.pr_b2 = dy.parameter(self.pr_pb2)

        # apply dropout
        if self.eval:
            self.disable_dropout()
        else:
            self.enable_dropout() 

        # initialize the RNNs
        self.f_init = self.fwdRNN.initial_state()
        self.b_init = self.bwdRNN.initial_state()

        self.cf_init = self.cfwdRNN.initial_state()
        self.cb_init = self.cbwdRNN.initial_state()

        self.ps_f_init = self.ps_fwdRNN.initial_state()
        self.ps_b_init = self.ps_bwdRNN.initial_state()

        self.pr_f_init = self.pr_fwdRNN.initial_state()
        self.pr_b_init = self.pr_bwdRNN.initial_state()

    def word_rep(self, w):
        if not self.eval and random.random() < 0.25:
            return self.LOOKUP_WORD[0]
        if args.lang == 'eng':
            idx = self.meta.w2i.get(w, self.meta.w2i.get(w.lower(), 0))
        else:
            idx = self.meta.w2i.get(w, 0)
        return self.LOOKUP_WORD[idx]

    def char_rep(self, w, f, b):
        bos, eos, unk = self.meta.c2i["bos"], self.meta.c2i["eos"], self.meta.c2i["unk"]
        char_ids = [bos] + [self.meta.c2i[c] if self.meta.cc[c]>5 else unk for c in w] + [eos]
        char_embs = [self.LOOKUP_CHAR[cid] for cid in char_ids]
        fw_exps = f.transduce(char_embs)
        bw_exps = b.transduce(reversed(char_embs))
        return dy.concatenate([ fw_exps[-1], bw_exps[-1] ])

    def get_char_embds(self, sentence, hf, hb):
        char_embs = []
        for node in sentence:
            char_embs.append(self.char_rep(node.form, hf, hb))
        return char_embs

    def get_word_embds(self, sentence):
        word_embs = []
        for node in sentence:
            word_embs.append(self.word_rep(node.form))
        return word_embs

    def basefeaturesEager(self, nodes, stack, i):
        #NOTE Stack nodes
        #s2 = nodes[stack[-3]] if stack[2:] else nodes[0].left
        #s1 = nodes[stack[-2]] if stack[1:] else nodes[0].left
        s0 = nodes[stack[-1]] if stack else nodes[0].left

        #NOTE Buffer nodes
        n0 = nodes[ i ] if nodes[ i: ] else nodes[0].left

        #NOTE Leftmost and Rightmost children of s2,s1,s0 and b0(only leftmost)
        #s2l = nodes[s2.left [-1]] if s2.left [-1] != None else nodes[0].left
        #s2r = nodes[s2.right[-1]] if s2.right[-1] != None else nodes[0].left
        #s1l = nodes[s1.left [-1]] if s1.left [-1] != None else nodes[0].left
        #s1r = nodes[s1.right[-1]] if s1.right[-1] != None else nodes[0].left
        #s0l = nodes[s0.left [-1]] if s0.left [-1] != None else nodes[0].left
        #s0r = nodes[s0.right[-1]] if s0.right[-1] != None else nodes[0].left
        #n0l = nodes[n0.left [-1]] if n0.left [-1] != None else nodes[0].left
        return [(nd.id, nd.form) for nd in (s0,n0)]
    
    def basefeaturesStandard(self, nodes, stack, i):
        #NOTE Stack nodes
        #s3 = nodes[stack[-4]] if stack[3:] else nodes[0].left
        #s2 = nodes[stack[-3]] if stack[2:] else nodes[0].left
        #s1 = nodes[stack[-2]] if stack[1:] else nodes[0].left
        s0 = nodes[stack[-1]] if stack else nodes[0].left

        #NOTE Buffer nodes
        n0 = nodes[ i ] if nodes[ i: ] else nodes[0].left
        #n0left = n0.left if i else [None]

        #NOTE Leftmost and Rightmost children of s2,s1,s0 and b0(only leftmost)
        #s3l = nodes[s3.left [-1]] if s3.left [-1] != None else nodes[0].left
        #s3r = nodes[s3.right[-1]] if s3.right[-1] != None else nodes[0].left
        #s2l = nodes[s2.left [-1]] if s2.left [-1] != None else nodes[0].left
        #s2r = nodes[s2.right[-1]] if s2.right[-1] != None else nodes[0].left
        #s1l = nodes[s1.left [-1]] if s1.left [-1] != None else nodes[0].left
        #s1r = nodes[s1.right[-1]] if s1.right[-1] != None else nodes[0].left
        #s0l = nodes[s0.left [-1]] if s0.left [-1] != None else nodes[0].left
        #s0r = nodes[s0.right[-1]] if s0.right[-1] != None else nodes[0].left
        #n0l = nodes[n0left [-1]]  if n0left  [-1] != None else nodes[0].left
        #n0r = nodes[n0.right[-1]] if n0.right[-1] != None else nodes[0].left
        
        return [(nd.id, nd.form) for nd in (s0,n0)]

    def feature_extraction(self, sentence):
        dy.renew_cg()
        self.initialize_graph_nodes()

        # get word/char embeddings
        wembs = self.get_word_embds(sentence)
        cembs = self.get_char_embds(sentence, self.cf_init, self.cb_init)
        lembs = [dy.concatenate([w,c]) for w,c in zip(wembs, cembs)]

        # feed word vectors into base biLSTM 
        fw_exps = self.f_init.transduce(lembs)
        bw_exps = self.b_init.transduce(reversed(lembs))
        bi_exps = [dy.concatenate([f,b]) for f,b in zip(fw_exps, reversed(bw_exps))]
    
        # feed biLSTM embeddings into POS biLSTM (pretrained)
        ps_fw_exps = self.ps_f_init.transduce(bi_exps)
        ps_bw_exps = self.ps_b_init.transduce(reversed(bi_exps))
        ps_bi_exps = [dy.concatenate([f,b]) for f,b in zip(ps_fw_exps, reversed(ps_bw_exps))]

        # get pos-hidden representation and pos loss
        pos_errs, pos_hidden = [], []
        for xi,node in zip(ps_bi_exps, sentence):
            xh = self.ps_W1 * xi
            pos_hidden.append(xh)
            xh = dy.rectify(xh) + self.ps_b1
            xo = self.ps_W2*xh + self.ps_b2
            #tid = self.meta.p2i[node.tag]
            err = dy.softmax(xo).npvalue() if self.eval else dy.pickneglogsoftmax(xo, self.meta.p2i[node.tag])
            pos_errs.append(err)

        # concatenate pos hidden-layer with base biLSTM 
        wcp_exps = [dy.concatenate([w,p]) for w,p in zip(bi_exps, pos_hidden)]
        # feed concatenated embeddings into parse biLSTM
        pr_fw_exps = self.pr_f_init.transduce(wcp_exps)
        pr_bw_exps = self.pr_b_init.transduce(reversed(wcp_exps))
        pr_bi_exps = [dy.concatenate([f,b]) for f,b in zip(pr_fw_exps, reversed(pr_bw_exps))]

        return pr_bi_exps, pos_errs

def Train(sentence, epoch, dynamic=True):
    loss = []
    totalError = 0
    parser.eval = False
    configuration = Configuration(sentence)
    pr_bi_exps, pos_errs = parser.feature_extraction(sentence[1:-1])
    while not parser.isFinalState(configuration):
        rfeatures = parser.basefeaturesEager(configuration.nodes, configuration.stack, configuration.b0)
        xi = dy.concatenate([pr_bi_exps[id-1] if id > 0 else parser.pad for id, rform in rfeatures])
        xh = parser.pr_W1 * xi
        xh = dy.rectify(xh) + parser.pr_b1
        xo = parser.pr_W2*xh + parser.pr_b2
        output_probs = dy.softmax(xo).npvalue()
        ranked_actions = sorted(zip(output_probs, range(len(output_probs))), reverse=True)
        pscore, paction = ranked_actions[0]

        validTransitions, allmoves = parser.get_valid_transitions(configuration) #{0: <bound method arceager.SHIFT>}
        while parser.action_cost(configuration, parser.meta.i2td[paction], parser.meta.transitions, validTransitions) > 500:
           ranked_actions = ranked_actions[1:]
           pscore, paction = ranked_actions[0]

        gaction = None
        for i,(score, ltrans) in enumerate(ranked_actions):
           cost = parser.action_cost(configuration, parser.meta.i2td[ltrans], parser.meta.transitions, validTransitions)
           if cost == 0:
              gaction = ltrans
              need_update = (i > 0)
              break

        gtransitionstr, goldLabel = parser.meta.i2td[gaction]
        ptransitionstr, predictedLabel = parser.meta.i2td[paction]
        if dynamic and (epoch > 2) and (np.random.random() < 0.9):
           predictedTransitionFunc = allmoves[parser.meta.transitions[ptransitionstr]]
           predictedTransitionFunc(configuration, predictedLabel)
        else:
           goldTransitionFunc = allmoves[parser.meta.transitions[gtransitionstr]]
           goldTransitionFunc(configuration, goldLabel)
        loss.append(dy.pickneglogsoftmax(xo, parser.meta.td2i[(gtransitionstr, goldLabel)])) #NOTE original

        if need_update: totalError += 1

    return dy.esum(loss) + dy.esum(pos_errs), totalError

def Test(parser, test_file):
    if not args.isDaemon:
        if args.outfile:
            ofile = io.open(args.outfile, 'w', encoding='utf-8')
        with io.open(test_file, encoding='utf-8') as fp:
            inputGenTest = re.finditer("(.*?)\n\n", fp.read(), re.S)
    else:
        inputGenTest = [test_file]

    parser.eval = True
    scores = defaultdict(int)
    good, bad = 0.0, 0.0
    for idx, sentence in enumerate(inputGenTest):
        if args.isDaemon:
            graph = list(depenencyGraph(sentence))
        else:
            graph = list(depenencyGraph(sentence.group(1)))
        pr_bi_exps, pos_errs = parser.feature_extraction(graph[1:-1])
        pred_pos = []
        for xo, node in zip(pos_errs, graph[1:-1]):
            p_tag = parser.meta.i2p[np.argmax(xo)]
            pred_pos.append(p_tag)
            if node.tag == p_tag:
                good += 1
            else:
                bad += 1

        configuration = Configuration(graph)
        while not parser.isFinalState(configuration):
            rfeatures = parser.basefeaturesEager(configuration.nodes, configuration.stack, configuration.b0)
            xi = dy.concatenate([pr_bi_exps[id-1] if id > 0 else parser.pad for id, rform in rfeatures])
            xh = parser.pr_W1 * xi
            xh = dy.rectify(xh) + parser.pr_b1
            xo = parser.pr_W2*xh + parser.pr_b2
            output_probs = dy.softmax(xo).npvalue()
            validTransitions, _ = parser.get_valid_transitions(configuration) #{0: <bound method arceager.SHIFT>}
            sortedPredictions = sorted(zip(output_probs, range(len(output_probs))), reverse=True)
            for score, action in sortedPredictions:
                transition, predictedLabel = parser.meta.i2td[action]
                if parser.meta.transitions[transition] in validTransitions:
                    predictedTransitionFunc = validTransitions[parser.meta.transitions[transition]]
                    predictedTransitionFunc(configuration, predictedLabel)
                    break
        dgraph = deprojectivize(graph[1:-1])
        if args.isDaemon:
            return dgraph, pred_pos, graph[0]
        scores = tree_eval(dgraph, scores)
        if args.outfile:
            for node in dgraph:
                ofile.write('\t'.join([unicode(node.id), node.form, u'_', node.tag, u'_', u'_',
                            unicode(node.pparent), node.pdrel.strip('%'), u'_', u'_'])+'\n')
            ofile.write(u'\n')
    sys.stderr.write('\n')

    UAS = round(100. * scores['rightAttach']/(scores['rightAttach']+scores['wrongAttach']),2)
    LS  = round(100. * scores['rightLabel']/(scores['rightLabel']+scores['wrongLabel']), 2)
    LAS = round(100. * scores['rightLabeledAttach']/(scores['rightLabeledAttach']+scores['wrongLabeledAttach']),2)
    
    return good/(good+bad), UAS, LS, LAS

def tree_eval(sentence, scores):
    for node in sentence:
        if node.parent == node.pparent:
            scores['rightAttach'] += 1
            if node.drel.strip('%') == node.pdrel.strip('%'):
                scores['rightLabeledAttach'] += 1
            else:
                scores['wrongLabeledAttach'] += 1
        else:
            scores['wrongAttach'] += 1
            scores['wrongLabeledAttach'] += 1

        if node.drel.strip('%') == node.pdrel.strip('%'):
            scores['rightLabel'] += 1
        else:
            scores['wrongLabel'] += 1
    return scores

def train_parser(dataset):
    n_samples = len(dataset)
    sys.stdout.write("Started training ...\n")
    sys.stdout.write("Training Examples: %s Classes: %s Epochs: %d\n\n" % (n_samples, parser.meta.n_outs, args.iter))
    psc, num_tagged, cum_loss = 0., 0, 0.
    for epoch in range(args.iter):
        random.shuffle(dataset)
        gtotalError, gtotal = 0, 0
        for sid, sentence in enumerate(dataset, 1):
            if sid % 500 == 0 or sid == n_samples:   # print status
                trainer.status()
                print(cum_loss / num_tagged)
                cum_loss, num_tagged = 0, 0
            sys.stdout.flush()
        csentence = copy.deepcopy(sentence)
        loss, totalError = Train(csentence, epoch+1)
        cum_loss += loss.scalar_value()
        num_tagged += 2 * len(sentence[1:-1]) - 1
        loss.backward()
        trainer.update()
        #sys.stderr.write("Training Instances:: %s\r"%sid)
        sys.stderr.flush()
        POS, UAS, LS, LAS = Test(parser, args.dev)
        sys.stderr.write("\nPOS ACCURACY: {}% UAS: {}%, LS: {}% and LAS: {}%\n".format(POS, UAS, LS, LAS))
        sys.stderr.flush()
        if LAS > psc:
            sys.stderr.write('SAVE POINT %d\n' %epoch)
            psc = LAS
            if args.save_model:
                parser.model.save('%s.dy' %args.save_model)
        #sys.stderr.write("Epoch:: %s Loss:: %s\n" % (epoch+1, 100.*gtotalError/gtotal))

def projective(nodes):
    """Identifies if a tree is non-projective or not."""
    for leaf1 in nodes:
        v1,v2 = sorted([int(leaf1.id), int(leaf1.parent)])
        for leaf2 in nodes:
            v3, v4 = sorted([int(leaf2.id), int(leaf2.parent)])
            if leaf1.id == leaf2.id:continue
            if (v1 < v3 < v2) and (v4 > v2): return False
    return True

def depenencyGraph(sentence):
    """Representation for dependency trees"""
    leaf = namedtuple('leaf', ['id','form','lemma','tag','ctag','features','parent','pparent', 'drel','pdrel','left','right', 'visit'])
    PAD = leaf._make([-1,'__PAD__','__PAD__','__PAD__','__PAD__',defaultdict(lambda:'__PAD__'),-1,-1,'__PAD__','__PAD__',[None],[None], False])
    yield leaf._make([0, 'ROOT_F', 'ROOT_L', 'ROOT_P', 'ROOT_C', defaultdict(str), -1, -1, '__ROOT__', '__ROOT__', PAD, [None], False])

    if args.isDaemon:
        for i,w in enumerate(sentence, 1):
            node = leaf._make([int(i),w,'_','_','_','_',-1,-1,'_','_',[None],[None], False])
            yield node
    else:
        for node in sentence.split("\n"):
            id_,form,lemma,tag,ctag,features,parent,drel = node.split("\t")[:8]
            node = leaf._make([int(id_),form,lemma,tag,ctag,features,int(parent),-1,drel,drel,[None],[None], False])
            yield node

    yield leaf._make([0, 'ROOT_F', 'ROOT_L', 'ROOT_P', 'ROOT_C', defaultdict(str), -1, -1, '__ROOT__', '__ROOT__', [None], [None], False])


def read(fname):
    with io.open(fname, encoding='utf-8') as fp:
        inputGenTrain = re.finditer("(.*?)\n\n", fp.read(), re.S)

    data = []
    for i,sentence in enumerate(inputGenTrain):
        graph = list(depenencyGraph(sentence.group(1)))
        try:
            pgraph = graph[:1]+projectivize(graph[1:-1])+graph[-1:]
        except:
            sys.stderr.write('Error Sent :: %d\n' %i)
            sys.stdout.flush()
            continue
        data.append(pgraph)
        for pnode in pgraph[1:-1]:
            for c in pnode.form:
                meta.cc[c] += 1
            plabels.add(pnode.tag)
            if pnode.parent == 0:
                tdlabels.add(('LEFTARC', pnode.drel))
            elif pnode.id < pnode.parent:
                tdlabels.add(('LEFTARC', pnode.drel))
            else:
                tdlabels.add(('RIGHTARC', pnode.drel))
    return data


class ArgumentParser():
    def __init__(self):
        self.ud = 1
        self.lang = None
        self.isDaemon = True

args = ArgumentParser()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="Neural Network Parser.", description="Bi-LSTM Parser")
    group = parser.add_mutually_exclusive_group()
    parser.add_argument('--dynet-mem')
    parser.add_argument('--dynet-devices')
    parser.add_argument('--dynet-autobatch')
    parser.add_argument('--dynet-gpu')
    parser.add_argument('--dynet-seed', dest='seed', type=int)
    parser.add_argument('--train', help='CONLL Train file')
    parser.add_argument('--dev', help='CONLL Dev file')
    parser.add_argument('--embd', help='Pretrained word2vec Embeddings')
    parser.add_argument('--lang')
    parser.add_argument('--trainer', help='Trainer [momsgd|adam|adadelta|adagrad]')
    parser.add_argument('--ud', type=int, default=1, help='1 if UD treebank else 0')
    parser.add_argument('--iter', type=int, default=100, help='No. of Epochs')
    parser.add_argument('--evec', type=int, help='1 if binary embedding file else 0')
    group.add_argument('--save-model', dest='save_model', help='Specify path to save model')
    group.add_argument('--load-model', dest='load_model', help='Load Pretrained Model')
    parser.add_argument('--retune-model', dest='retune_model', help='Retune pretrained model')
    parser.add_argument('--output-file', dest='outfile', help='Output File')
    parser.add_argument('--daemonize', dest='isDaemon', action='store_true', default = False)
    parser.add_argument('--port', type=int, dest='daemonPort', help='Specify a port number')
    args = parser.parse_args()
    np.random.seed(args.seed)
    random.seed(args.seed)

    meta = Meta()
    if not args.load_model:
        plabels = set()
        tdlabels = set()
        meta.cc = Counter(['bos', 'eos', 'unk'])
        tdlabels.add(('SHIFT', None))
        tdlabels.add(('REDUCE', None))

        train_sents = read(args.train)
        wvm = Word2Vec.load_word2vec_format(args.embd, binary=args.evec)
        meta.w_dim = wvm.syn0.shape[1]
        meta.n_words = wvm.syn0.shape[0]+meta.add_words

        meta.i2p = dict(enumerate(plabels))
        meta.i2td = dict(enumerate(tdlabels))
        meta.p2i = {v: k for k,v in meta.i2p.iteritems()}
        meta.td2i = {v: k for k,v in meta.i2td.iteritems()}
        meta.n_outs = len(meta.i2td)
        meta.n_tags = len(meta.p2i)
        meta.c2i = {c: i for i,c in enumerate(meta.cc.keys())}
        meta.n_chars = len(meta.c2i)

        meta.w2i = {}
        for w in wvm.vocab:
            meta.w2i[w] = wvm.vocab[w].index + meta.add_words

    if args.save_model:
        pickle.dump(meta, open('%s.meta' %args.save_model, 'wb'))
    if args.load_model:
        #sys.stderr.write('Loading Models ...\n')
        parser = Parser(model=args.load_model, test=True)
        #sys.stderr.write('Shoot!\n')
        POS, UAS, LS, LAS = Test(parser, args.dev)
        sys.stderr.write("TEST-SET POS: {}%, UAS: {}%, LS: {}% and LAS: {}%\n".format(POS, UAS, LS, LAS))
    elif args.retune_model:
        parser = Parser(model=args.retune_model)
        trainer = dy.MomentumSGDTrainer(parser.model)
        train_parser(train_sents)
    else:
        parser = Parser(meta=meta)
        trainer = dy.MomentumSGDTrainer(parser.model)
        train_parser(train_sents)
