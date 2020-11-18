#!/usr/bin/env python3
# Copyright (c) 2018-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import torch as th
import numpy as np
import logging
import argparse
from hype.sn import Embedding, initialize
from hype.adjacency_matrix_dataset import AdjacencyDataset
from hype import train
from hype.graph import load_adjacency_matrix, load_edge_list, eval_reconstruction
from hype.rsgd import RiemannianSGD
from hype.lorentz import LorentzManifold
from hype.lorentz_product import LorentzProductManifold
from hype.group_rie import GroupRieManifold
from hype.group_rie_high import GroupRiehighManifold
from hype.bugaenko6 import Bugaenko6Manifold
from hype.vinberg17 import Vinberg17Manifold
from hype.group_euc import GroupEucManifold
from hype.halfspace_rie import HalfspaceRieManifold
from hype.euclidean import EuclideanManifold
from hype.poincare import PoincareManifold
import sys
import json
import torch.multiprocessing as mp



th.manual_seed(42)
np.random.seed(42)


MANIFOLDS = {
    'lorentz': LorentzManifold,
    'lorentz_product': LorentzProductManifold,
    'group_rie': GroupRieManifold,
    'group_rie_high': GroupRiehighManifold,
    'bugaenko6': Bugaenko6Manifold,
    'vinberg17': Vinberg17Manifold,
    'group_euc': GroupEucManifold,
    'halfspace_rie': HalfspaceRieManifold,
    'euclidean': EuclideanManifold,
    'poincare': PoincareManifold
}


# Adapated from:
# https://thisdataguy.com/2017/07/03/no-options-with-argparse-and-python/
class Unsettable(argparse.Action):
    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        super(Unsettable, self).__init__(option_strings, dest, nargs='?', **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        val = None if option_string.startswith('-no') else values
        setattr(namespace, self.dest, val)


def main():    
    parser = argparse.ArgumentParser(description='Train Hyperbolic Embeddings')
    parser.add_argument('-dset', type=str, required=True,
                        help='Dataset identifier')
    parser.add_argument('-dim', type=int, default=20,
                        help='Embedding dimension')
    parser.add_argument('-manifold', type=str, default='lorentz',
                        choices=MANIFOLDS.keys(), help='Embedding manifold')
    parser.add_argument('-lr', type=float, default=1000,
                        help='Learning rate')
    parser.add_argument('-epochs', type=int, default=100,
                        help='Number of epochs')
    parser.add_argument('-batchsize', type=int, default=12800,
                        help='Batchsize')
    parser.add_argument('-negs', type=int, default=50,
                        help='Number of negatives')
    parser.add_argument('-burnin', type=int, default=20,
                        help='Epochs of burn in')
    parser.add_argument('-dampening', type=float, default=0.75,
                        help='Sample dampening during burnin')
    parser.add_argument('-ndproc', type=int, default=8,
                        help='Number of data loading processes')
    parser.add_argument('-eval_each', type=int, default=1,
                        help='Run evaluation every n-th epoch')
    parser.add_argument('-norevery', type=int, default=50,
                        help='normalize every n-th epoch')
    parser.add_argument('-debug', action='store_true', default=False,
                        help='Print debuggin output')
    parser.add_argument('-gpu', default=-1, type=int,
                        help='Which GPU to run on (-1 for no gpu)')
    parser.add_argument('-sym', action='store_true', default=False,
                        help='Symmetrize dataset')
    parser.add_argument('-maxnorm', '-no-maxnorm', default='500000',
                        action=Unsettable, type=int)
    parser.add_argument('-sparse', default=False, action='store_true',
                        help='Use sparse gradients for embedding table')
    parser.add_argument('-burnin_multiplier', default=0.01, type=float)
    parser.add_argument('-neg_multiplier', default=1.0, type=float)
    parser.add_argument('-quiet', action='store_true', default=True)
    parser.add_argument('-lr_type', choices=['scale', 'constant'], default='constant')
    parser.add_argument('-train_threads', type=int, default=1,
                        help='Number of threads to use in training')
    parser.add_argument('-eval_embedding', default=False, help='path for the embedding to be evaluated')
    opt = parser.parse_args()
    
    if 'group' in opt.manifold:
        opt.nor = 'group'
        opt.norevery = 20
        opt.stre = 50
    elif 'bugaenko6' in opt.manifold:
        opt.nor = 'bugaenko6'
        opt.stre = 0
        opt.norevery = 10
    elif 'vinberg17' in opt.manifold:
        opt.nor = 'vinberg17'
        opt.stre = 50
    elif 'halfspace' in opt.manifold:
        opt.nor = 'halfspace'
        opt.norevery = 1
        opt.stre = 0
    else:
        opt.nor = 'none'

    # setup debugging and logigng
    log_level = logging.DEBUG if opt.debug else logging.INFO
    log = logging.getLogger('tiling model')
    logging.basicConfig(level=log_level, format='%(message)s', stream=sys.stdout)

    # set default tensor type
    th.set_default_tensor_type('torch.DoubleTensor')####FloatTensor DoubleTensor
    #set device
    device = th.device(f'cuda:{opt.gpu}' if opt.gpu >= 0 else 'cpu')
    #device = th.device('cpu')

    # select manifold to optimize on
    manifold = MANIFOLDS[opt.manifold](debug=opt.debug, max_norm=opt.maxnorm)
    opt.dim = manifold.dim(opt.dim)

    if 'csv' in opt.dset:
        log.info('Using edge list dataloader')
        idx, objects, weights = load_edge_list(opt.dset, opt.sym)
        model, data, model_name, conf = initialize(
            manifold, opt, idx, objects, weights, device, sparse=opt.sparse 
        )
    else:
        log.info('Using adjacency matrix dataloader')
        dset = load_adjacency_matrix(opt.dset, 'hdf5')
        log.info('Setting up dataset...')
        data = AdjacencyDataset(dset, opt.negs, opt.batchsize, opt.ndproc,
            opt.burnin > 0, sample_dampening=opt.dampening)
        model = Embedding(data.N, opt.dim, manifold, sparse=opt.sparse)
        objects = dset['objects']

    # set burnin parameters
    data.neg_multiplier = opt.neg_multiplier
    train._lr_multiplier = opt.burnin_multiplier
    # Build config string for log
    log.info(f'json_conf: {json.dumps(vars(opt))}')
    if opt.lr_type == 'scale':
        opt.lr = opt.lr * opt.batchsize

    # setup optimizer
    optimizer = RiemannianSGD(model.optim_params(manifold), lr=opt.lr)
    opt.epoch_start = 0
    adj = {}
    for inputs, _ in data:
        for row in inputs:
            x = row[0].item()
            y = row[1].item()
            if x in adj:
                adj[x].add(y)
            else:
                adj[x] = {y}

    #trainig 
    if not opt.eval_embedding:
        opt.adj = adj
        model = model.to(device)
        if hasattr(model, 'w_avg'):
            model.w_avg = model.w_avg.to(device)
        if opt.train_threads > 1:
            threads = []
            model = model.share_memory()
            if 'group' in opt.manifold or 'bugaenko6' in opt.manifold or 'vinberg17' in opt.manifold:
                model.int_matrix.share_memory_()
            args = (device, model, data, optimizer, opt, log)
            kwargs = {'progress' : not opt.quiet}
            for i in range(opt.train_threads):
                threads.append(mp.Process(target=train.train, args=args, kwargs=kwargs))
                threads[-1].start()
            [t.join() for t in threads]
        else:
            train.train(device, model, data, optimizer, opt, log, progress=not opt.quiet)
    else:
        model = th.load(opt.eval_embedding, map_location='cpu')['embeddings']
    
    #evaluation
    if 'group' in opt.manifold:
        meanrank, maprank = eval_reconstruction(adj, model.lt.weight.data.clone(), manifold.distance, lt_int_matrix = model.int_matrix.data.clone())
        sqnorms = manifold.pnorm(model.lt.weight.data.clone(), model.int_matrix.data.clone())
    elif 'bugaenko6' in opt.manifold or 'vinberg17' in opt.manifold: 
        meanrank, maprank = eval_reconstruction(adj, model.lt.weight.data.clone(), manifold.distance, g = model.g, lt_int_matrix = model.int_matrix.data.clone())
        sqnorms = manifold.pnorm(model.lt.weight.data.clone(), model.int_matrix.data.clone())
    else:
        meanrank, maprank = eval_reconstruction(adj, model.lt.weight.data.clone(), manifold.distance)
        sqnorms = manifold.pnorm(model.lt.weight.data.clone())
    
    log.info(
        'json_stats: {'
        f'"sqnorm_min": {sqnorms.min().item()}, '
        f'"sqnorm_avg": {sqnorms.mean().item()}, '
        f'"sqnorm_max": {sqnorms.max().item()}, '
        f'"mean_rank": {meanrank}, '
        f'"map_rank": {maprank}, '
        '}'
    )
    

if __name__ == '__main__':
    main()
