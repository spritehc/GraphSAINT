from graphsaint.globals import *
import math
from graphsaint.inits import *
from graphsaint.utils import *
from graphsaint.graph_samplers import *
from graphsaint.adj_misc import *
import tensorflow as tf
import scipy.sparse as sp
import pandas as pd

import numpy as np
import time
import multiprocessing as mp

import pdb

# import warnings
# warnings.filterwarnings('error')
# np.seterr(divide='raise')

#np.random.seed(123)


############################
# FOR SUPERVISED MINIBATCH #
############################
class NodeMinibatchIterator(object):
    """
    This minibatch iterator iterates over nodes for supervised learning.
    """

    def __init__(self, adj_full, adj_full_norm, adj_train, role, class_arr, placeholders, train_params, **kwargs):
        """
        role:       array of string (length |V|)
                    storing role of the node ('tr'/'va'/'te')
        class_arr: array of float (shape |V|xf)
                    storing initial feature vectors
        TODO:       adj_full_norm: normalized adj. Norm by # non-zeros per row

        norm_aggr_train:        list of dict
                                norm_aggr_train[u] gives a dict recording prob of being sampled for all u's neighbor
                                norm_aggr_train[u][v] gives the prob of being sampled for edge (u,v). i.e. P_{sampler}(u sampled|v sampled)
        """
        self.num_proc = 1
        self.node_train = np.array(role['tr'])
        self.node_val = np.array(role['va'])
        self.node_test = np.array(role['te'])

        self.class_arr = class_arr
        self.adj_full = adj_full
        self.adj_full_norm = adj_full_norm
        s1=int(adj_full_norm.shape[0]/4)
        s2=int(adj_full_norm.shape[0]/2)
        s3=int(adj_full_norm.shape[0]/4*3)
        self.adj_full_norm_0=adj_full_norm[:s1,:]
        self.adj_full_norm_1=adj_full_norm[s1:s2,:]
        self.adj_full_norm_2=adj_full_norm[s2:s3,:]
        self.adj_full_norm_3=adj_full_norm[s3:,:]
        self.adj_train = adj_train

        assert self.class_arr.shape[0] == self.adj_full.shape[0]

        # below: book-keeping for mini-batch
        self.placeholders = placeholders
        self.node_subgraph = None
        self.batch_num = -1

        self.method_sample = None
        self.subgraphs_remaining_indptr = []
        self.subgraphs_remaining_indices = []
        self.subgraphs_remaining_indices_orig = []
        self.subgraphs_remaining_data = []
        self.subgraphs_remaining_nodes = []
        
        self.norm_loss_train = None
        self.norm_loss_test = np.zeros(self.adj_full.shape[0])
        self.norm_loss_test[self.node_train] = 1
        self.norm_loss_test[self.node_val] = 1
        self.norm_loss_test[self.node_test] = 1
        self.norm_aggr_train = [dict()]     # list of dict. List index: start node index. dict key: end node idx
        #self.norm_aggr_test = dict()
        #for u in range(self.adj_test.shape[0]):
        #    _ind_start = self.adj_full_norm.indptr[u]
        #    _ind_end = self.adj_full_norm.indptr[u+1]
        #    for v in self.adj_full_norm.indices[_ind_start:_ind_end]:
        #        self.norm_aggr_test[(u,v)] = 1
        
        self.norm_adj = train_params['norm_adj']
        self.q_threshold = train_params['q_threshold']
        self.q_offset = train_params['q_offset']

    def _set_sampler_args(self):
        if self.method_sample == 'frontier':
            _args = {'frontier':None}
        elif self.method_sample == 'khop':
            _args = dict()
        elif self.method_sample == 'rw':
            _args = dict()
        elif self.method_sample == 'edge':
            _args = dict()
        else:
            raise NotImplementedError
        return _args


    def set_sampler(self,train_phases,is_norm_loss=False,is_norm_aggr=False):
        self.subgraphs_remaining_indptr = list()
        self.subgraphs_remaining_indices = list()
        self.subgraphs_remaining_indices_orig = list()
        self.subgraphs_remaining_data = list()
        self.subgraphs_remaining_nodes = list()
        self.method_sample = train_phases['sampler']
        if self.method_sample == 'frontier':
            self.size_subg_budget = train_phases['size_subgraph']
            self.graph_sampler = frontier_sampling(self.adj_train,self.adj_full,\
                self.node_train,self.size_subg_budget,dict(),train_phases['size_frontier'],int(train_phases['order']),int(train_phases['max_deg']))
        elif self.method_sample == 'khop':
            self.size_subg_budget = train_phases['size_subgraph']
            self.graph_sampler = khop_sampling(self.adj_train,self.adj_full,\
                self.node_train,self.size_subg_budget,dict(),int(train_phases['order']))
        elif self.method_sample == 'rw':
            self.size_subg_budget = train_phases['num_root']*train_phases['depth']
            self.graph_sampler = rw_sampling(self.adj_train,self.adj_full,\
                self.node_train,self.size_subg_budget,dict(),int(train_phases['num_root']),int(train_phases['depth']))
        elif self.method_sample == 'edge':
            self.size_subg_budget = train_phases['size_subgraph']
            self.graph_sampler = edge_sampling(self.adj_train,self.adj_full,self.node_train,self.size_subg_budget,dict())
        else:
            raise NotImplementedError

        self.norm_loss_train = np.zeros(self.adj_full.shape[0])
        if not is_norm_loss and not is_norm_aggr:
            self.norm_loss_train[self.node_train] = 1
        else:
            subg_end = 0; tot_sampled_nodes = 0
            if is_norm_aggr:
                self.norm_aggr_train = list()
                for u in range(self.adj_train.shape[0]):
                    self.norm_aggr_train.append(dict())
                    _ind_start = self.adj_train.indptr[u]
                    _ind_end = self.adj_train.indptr[u+1]
                    for v in self.adj_train.indices[_ind_start:_ind_end]:
                        self.norm_aggr_train[u][v] = 0
            while True:
                self.par_graph_sample('train')
                for i in range(subg_end,len(self.subgraphs_remaining_nodes)):
                    # you can check whether these subgrpahs_remaining_nodes are sorted already
                    self.norm_loss_train[self.subgraphs_remaining_nodes[i]] += 1
                    assert len(self.subgraphs_remaining_indptr[i]) == len(self.subgraphs_remaining_nodes[i])+1
                    for ip in range(len(self.subgraphs_remaining_nodes[i])):
                        _u = self.subgraphs_remaining_nodes[i][ip]
                        for _v in self.subgraphs_remaining_indices_orig[i][self.subgraphs_remaining_indptr[i][ip]:self.subgraphs_remaining_indptr[i][ip+1]]:
                            self.norm_aggr_train[_u][_v] += 1
                    tot_sampled_nodes += self.subgraphs_remaining_nodes[i].size
                    # need re-map from new index to original index?
                if tot_sampled_nodes > self.q_threshold*self.node_train.size:
                    break
                subg_end = len(self.subgraphs_remaining_nodes)
            num_subg = len(self.subgraphs_remaining_nodes)
            # normalize count in norm_aggr_train
            for i_d,d in enumerate(self.norm_aggr_train):
                self.norm_aggr_train[i_d] = {k:v/max(self.norm_loss_train[i_d],0.01) for k,v in d.items()}
            self.norm_loss_train[self.node_train] += self.q_offset
            if self.norm_loss_train[self.node_train].min() == 0:
                self.norm_loss_train[self.node_train] += 1
            self.norm_loss_train[self.node_train] = 1/self.norm_loss_train[self.node_train]
            avg_subg_size = tot_sampled_nodes/num_subg
            self.norm_loss_train = self.norm_loss_train\
                        /self.norm_loss_train.sum()*self.node_train.size
           


    def par_graph_sample(self,phase):
        t0 = time.time()
        # _indices_orig: subgraph with indices in the original graph
        _indptr,_indices,_indices_orig,_data,_v = self.graph_sampler.par_sample(phase,**self._set_sampler_args())
        t1 = time.time()
        print('sampling 200 subgraphs:   time = ',t1-t0)
        self.subgraphs_remaining_indptr.extend(_indptr)
        self.subgraphs_remaining_indices.extend(_indices)
        self.subgraphs_remaining_indices_orig.extend(_indices_orig)
        self.subgraphs_remaining_data.extend(_data)
        self.subgraphs_remaining_nodes.extend(_v)

    def minibatch_train_feed_dict(self,dropout,is_val=False,is_test=False):
        """ DONE """
        if is_val or is_test:
            self.node_subgraph = np.arange(self.class_arr.shape[0])
            adj = sp.csr_matrix(([],[],np.zeros(2)), shape=(1,self.node_subgraph.shape[0]))
            adj_0 = self.adj_full_norm_0
            adj_1 = self.adj_full_norm_1
            adj_2 = self.adj_full_norm_2
            adj_3 = self.adj_full_norm_3
        else:
            if len(self.subgraphs_remaining_nodes) == 0:
                self.par_graph_sample('train')

            self.node_subgraph = self.subgraphs_remaining_nodes.pop()
            self.size_subgraph = len(self.node_subgraph)
            adj = sp.csr_matrix((self.subgraphs_remaining_data.pop(),self.subgraphs_remaining_indices.pop(),\
                        self.subgraphs_remaining_indptr.pop()),shape=(self.node_subgraph.size,self.node_subgraph.size))
            adj = adj_norm(adj,self.norm_adj)       # TODO: additional arg: is_norm_aggr, aggr_norm_vector
            adj_0 = sp.csr_matrix(([],[],np.zeros(2)),shape=(1,self.node_subgraph.shape[0]))
            adj_1 = sp.csr_matrix(([],[],np.zeros(2)),shape=(1,self.node_subgraph.shape[0]))
            adj_2 = sp.csr_matrix(([],[],np.zeros(2)),shape=(1,self.node_subgraph.shape[0]))
            adj_3 = sp.csr_matrix(([],[],np.zeros(2)),shape=(1,self.node_subgraph.shape[0]))
            self.batch_num += 1
        feed_dict = dict()
        feed_dict.update({self.placeholders['node_subgraph']: self.node_subgraph})
        feed_dict.update({self.placeholders['labels']: self.class_arr[self.node_subgraph]})
        feed_dict.update({self.placeholders['dropout']: dropout})
        if is_val or is_test:
            feed_dict.update({self.placeholders['norm_loss']: self.norm_loss_test})
        else:
            feed_dict.update({self.placeholders['norm_loss']:self.norm_loss_train})
        
        _num_edges = len(adj.nonzero()[1])
        _num_vertices = len(self.node_subgraph)
        _indices_ph = np.column_stack(adj.nonzero())
        _shape_ph = adj.shape
        feed_dict.update({self.placeholders['adj_subgraph']: \
            tf.SparseTensorValue(_indices_ph,adj.data,_shape_ph)})
        feed_dict.update({self.placeholders['adj_subgraph_0']: \
            tf.SparseTensorValue(np.column_stack(adj_0.nonzero()),adj_0.data,adj_0.shape)})
        feed_dict.update({self.placeholders['adj_subgraph_1']: \
            tf.SparseTensorValue(np.column_stack(adj_1.nonzero()),adj_1.data,adj_1.shape)})
        feed_dict.update({self.placeholders['adj_subgraph_2']: \
            tf.SparseTensorValue(np.column_stack(adj_2.nonzero()),adj_2.data,adj_2.shape)})
        feed_dict.update({self.placeholders['adj_subgraph_3']: \
            tf.SparseTensorValue(np.column_stack(adj_3.nonzero()),adj_3.data,adj_3.shape)})
        feed_dict.update({self.placeholders['nnz']: adj.size})
        if is_val or is_test:
            feed_dict[self.placeholders['is_train']]=False
        else:
            feed_dict[self.placeholders['is_train']]=True
        return feed_dict, self.class_arr[self.node_subgraph]


    def num_training_batches(self):
        """ DONE """
        return math.ceil(self.node_train.shape[0]/float(self.size_subg_budget))

    def shuffle(self):
        """ DONE """
        self.node_train = np.random.permutation(self.node_train)
        self.batch_num = -1

    def end(self):
        """ DONE """
        return (self.batch_num+1)*self.size_subg_budget >= self.node_train.shape[0]
