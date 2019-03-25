#! /usr/bin/env python
import argparse, sys, os, errno
import logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] [%(levelname)s] %(name)s: %(message)s')

import numpy as np
'''
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
import seaborn as sns
sns.set()
'''
import pandas as pd
from pandas import DataFrame, Series
from scipy.fftpack import fft
from scipy.signal import convolve
import numba

command_handlers = {}
def command_handler(f):
    command_handlers[f.__name__] = f
    return f

def read_coverage(filename):
    coverage = []
    gene_ids = []
    with open(filename, 'r') as f:
        for line in f:
            c = line.strip().split('\t')
            gene_id = c[0]
            values = np.array(c[1:]).astype(np.float64)
            gene_ids.append(gene_id)
            coverage.append(values)
    return gene_ids, coverage

@numba.jit('int64(int32[:], int32[:], float64, float64, float64)')
def icm_update(x, y, h=0.0, beta=1.0, eta=2.1):
    n_changes = 0
    N = x.shape[0]
    for i in range(N):
        dx = -2*x[i]
        dE = 0
        if i > 0:
            dE += h*dx - beta*dx*x[i - 1] - eta*dx*y[i]
        if i < (N - 1):
            dE += h*dx - beta*dx*x[i + 1] - eta*dx*y[i]
        if dE < 0:
            x[i] = -x[i]
            n_changes += 1
    return n_changes
        
def icm_smooth(x, h=0.0, beta=1.0, eta=2.1):
    '''Smooth signals using iterated conditional modes
    Args:
        x: 1D signal
    Returns:
        Smoothed signal of the same length of x
    '''
    x = x*2 - 1
    y = x.copy()
    #E = h*np.sum(x) - beta*x[:-1]*x[1:] - eta*x*y
    n_updates = icm_update(x, y, h=h, beta=beta, eta=eta)
    while n_updates > 0:
        n_updates = icm_update(x, y, h=h, beta=beta, eta=eta)
    x = (x > 0).astype(np.int32)
    return x

def call_peak_gene(sig, local_bg_width=3, local_bg_weight=0.5, bg_global=None, smooth=False):
    if bg_global is None:
        bg_global = np.mean(sig)

    filter = np.full(local_bg_width, 1.0/local_bg_width)
    bg_local = convolve(sig, filter, mode='same')
    bg = local_bg_weight*bg_local + (1.0 - local_bg_weight)*bg_global
    bg[np.isclose(bg, 0)] = 1
    snr = sig/bg
    peaks = (snr > 1.0).astype(np.int32)
    if smooth:
        peaks = icm_smooth(peaks, h=-2.0, beta=4.0, eta=2.0)
    x = np.zeros(len(peaks) + 2, dtype=np.int32)
    x[1:-1] = peaks
    starts = np.nonzero(x[1:] > x[:-1])[0]
    ends = np.nonzero(x[:-1] > x[1:])[0]
    peaks = np.column_stack([starts, ends])
    return peaks

def estimate_bg_global(signals):
    '''signals
    '''
    signals_mean = np.asarray([np.mean(s) for s in signals])
    bg = np.median(signals_mean)
    return bg

def call_peaks(signals, min_length=2):
    #bg_global = estimate_bg_global(signals)
    bg_global = None
    peaks = []
    for i, signal in enumerate(signals):
        peak_locations = call_peak_gene(signal, bg_global=bg_global, smooth=True)
        #print(signal)
        for start, end in peak_locations:
            #print(peak_locations)
            if (min_length is None) or ((end - start) >= min_length):
                peaks.append((i, start, end, signal[start:end].mean()))
    return peaks

@command_handler
def call_peak(args):
    from tqdm import trange

    logger.info('read input file: ' + args.input_file)
    gene_ids, signals = read_coverage(args.input_file)
    if args.use_log:
        signals = [np.log10(np.maximum(1e-3, a)) + 3 for a in signals]

    signals_mean = np.asarray([np.mean(a) for a in signals])
    bg_global = np.median(signals_mean)

    logger.info('create output plot file: ' + args.output_file)
    with open(args.output_file, 'w') as fout:
        for i in trange(len(signals), unit='gene'):
            peaks_locations = call_peak_gene(signals[i], bg_global=bg_global, local_bg_weight=args.local_bg_weight,
                local_bg_width=args.local_bg_width, smooth=args.smooth)
            for j in range(peaks_locations.shape[0]):
                fout.write('{}\t{}\t{}\n'.format(gene_ids[i], peaks_locations[j, 0], peaks_locations[j, 1]))

@command_handler
def refine_peaks(args):
    import pandas as pd
    import numpy as np
    from tqdm import tqdm
    import re
    import h5py
    #from bx.bbi.bigwig_file import BigWigFile
    from pykent import BigWigFile
    from ioutils import open_file_or_stdout


    logger.info('read input matrix file: ' + args.peaks)
    #matrix = pd.read_table(args.matrix, sep='\t', index_col=0)
    #feature_info = matrix.index.to_series().str.split('|', expand=True)
    #feature_info.columns = ['gene_id', 'gene_type', 'gene_name', 'domain_id', 'transcript_id', 'start', 'end']
    #feature_info['start'] = feature_info['start'].astype('int')
    #feature_info['end'] = feature_info['end'].astype('int')
    peaks = pd.read_table(args.peaks, sep='\t', header=None, dtype=str)
    if peaks.shape[1] < 6:
        raise ValueError('less than 6 columns in peak file')
    peaks.columns = ['chrom', 'start', 'end', 'name', 'score', 'strand'] + ['c%d'%i for i in range(6, peaks.shape[1])]
    peaks['start'] = peaks['start'].astype('int')
    peaks['end'] = peaks['end'].astype('int')

    logger.info('read chrom sizes: ' + args.chrom_sizes)
    chrom_sizes = pd.read_table(args.chrom_sizes, sep='\t', header=None, names=['chrom', 'size'])
    chrom_sizes = chrom_sizes.drop_duplicates('chrom')
    chrom_sizes = chrom_sizes.set_index('chrom').iloc[:, 0]

    logger.info('read input genomic bigwig file: ' + args.tbigwig)
    tbigwig = BigWigFile(args.tbigwig)
    #chrom_sizes.update(dict(tbigwig.get_chrom_sizes()))

    gbigwig = {}
    logger.info('read input genomic bigwig (+) file: ' + args.gbigwig_plus)
    gbigwig['+'] = BigWigFile(args.gbigwig_plus)
    logger.info('read input genomic bigwig (-) file: ' + args.gbigwig_minus)
    gbigwig['-'] = BigWigFile(args.gbigwig_minus)
    #chrom_sizes.update(dict(gbigwig['+'].get_chrom_sizes()))

    flanking = args.flanking
    signals = []
    signals_mean = []
    windows = []
    pat_gene_id = re.compile('^(.*)_([0-9]+)_([0-9]+)_([+-])$')
    for _, peak in peaks.iterrows():
        if peak['chrom'].startswith('chr'):
        #if feature['gene_type'] == 'genomic':
            #chrom, start, end, strand = pat_gene_id.match(feature['gene_id']).groups()
            #start = int(start)
            #end = int(end)
            window_start = max(0, peak['start'] - flanking)
            window_end = min(peak['end'] + flanking, chrom_sizes[peak['chrom']])
            data = gbigwig[peak['strand']].query_interval(peak['chrom'], window_start, window_end, fillna=0)
        else:
            strand = '+'
            window_start = max(0, peak['start'] - flanking)
            window_end = min(peak['end'] + flanking, chrom_sizes[peak['chrom']])
            data = tbigwig.query_interval(peak['chrom'], window_start, window_end, fillna=0)
        if data is None:
            data = np.zeros((window_end - window_start))
            #logger.info('no coverage data found for peak: {}'.format(feature['domain_id']))
        if args.use_log:
            data = np.log2(np.maximum(data, 0.25)) + 2
        signals.append(data)
        signals_mean.append(np.mean(data))
        windows.append((peak['chrom'], window_start, window_end, peak['start'], peak['end'], peak['strand']))
    windows = pd.DataFrame.from_records(windows)
    windows.columns = ['chrom', 'window_start', 'window_end', 'start', 'end', 'strand']

    logger.info('call peaks')
    refined_peaks = call_peaks(signals, min_length=args.min_length)
    with open_file_or_stdout(args.output_file) as fout:
        for i, start, end, mean_signal in refined_peaks:
            # map peak coordinates from window to original
            peak = [windows['chrom'][i], 
                start + windows['start'][i],
                end + windows['start'][i],
                'peak_%d'%(i + 1),
                '%.4f'%mean_signal,
                strand
            ]
            # remove peaks not overlapping with the window
            if (peak[1] > windows['end'][i]) or (peak[2] < windows['start'][i]):
                continue
            fout.write('\t'.join(map(str, peak)) + '\n')
        #print('%s\t%d\t%d => %s\t%d\t%d'%(
        #    windows['chrom'][i], windows['start'][i], windows['end'][i],
        #    windows['chrom'][i], start, end))


if __name__ == '__main__':
    main_parser = argparse.ArgumentParser(description='Call peaks from exRNA signals')
    subparsers = main_parser.add_subparsers(dest='command')
    
    parser = subparsers.add_parser('call_peak')
    parser.add_argument('--input-file', '-i', type=str, required=True,
        help='input file of exRNA signals for each transcript')
    parser.add_argument('--use-log', action='store_true', 
        help='use log10 instead raw signals')
    parser.add_argument('--smooth', action='store_true',
        help='merge adjacent peaks')
    parser.add_argument('--local-bg-width', type=int, default=3,
        help='number of nearby bins for estimation of local background')
    parser.add_argument('--local-bg-weight', type=float, default=0.5, 
        help='weight for local background (0.0-1.0)')
    parser.add_argument('--output-file', '-o', type=str, required=True,
        help='output plot file BED format')
    
    parser = subparsers.add_parser('refine_peaks')
    parser.add_argument('--peaks', type=str, required=True,
        help='input count matrix with feature names as the first column')
    parser.add_argument('--tbigwig', type=str, required=True,
        help='transcript BigWig file')
    parser.add_argument('--gbigwig-plus', type=str, required=True,
        help='genomic BigWig (+) file')
    parser.add_argument('--gbigwig-minus', type=str, required=True,
        help='genomic BigWig (-) file')
    parser.add_argument('--chrom-sizes', type=str, required=True,
        help='chrom sizes')
    parser.add_argument('--output-file', '-o', type=str, default='-',
        help='output refined peaks')
    parser.add_argument('--use-log', action='store_true', 
        help='use log10 instead raw signals')
    parser.add_argument('--smooth', action='store_true',
        help='merge adjacent peaks')
    parser.add_argument('--local-bg-width', type=int, default=3,
        help='number of nearby bins for estimation of local background')
    parser.add_argument('--local-bg-weight', type=float, default=0.5, 
        help='weight for local background (0.0-1.0)')
    parser.add_argument('--flanking', type=int, default=20)
    parser.add_argument('--min-length', type=int, default=10)

    args = main_parser.parse_args()
    if args.command is None:
        raise ValueError('empty command')
    logger = logging.getLogger('call_peak.' + args.command)

    command_handlers.get(args.command)(args)

