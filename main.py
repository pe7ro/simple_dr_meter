#!/usr/bin/env python3
import argparse
import functools
import os
import sys
import time
from datetime import datetime
from typing import Iterable, Tuple, NamedTuple

import numpy

from audio_io import read_audio_info, read_audio_data
from audio_io.audio_io import AudioSourceInfo, AudioSource
from audio_metrics import compute_dr
from util.constants import MEASURE_SAMPLE_RATE


def get_log_path(in_path):
    if os.path.isdir(in_path):
        out_path = in_path
    else:
        out_path = os.path.dirname(in_path)
    return os.path.join(out_path, 'dr.txt')


class LogGroup(NamedTuple):
    performers: Iterable[str]
    albums: Iterable[str]
    channels: int
    sample_rate: int
    tracks_dr: Iterable[Tuple[int, float, float, int, str]]


def get_group_title(group: LogGroup):
    return f'{", ".join(group.performers)} — {", ".join(group.albums)}'


def format_time(seconds):
    d = divmod
    m, s = d(seconds, 60)
    h, m = d(m, 60)
    if h:
        return f'{h}:{m:02d}:{s:02d}'
    return f'{m}:{s:02d}'


def write_log(out_path, dr_log_groups: Iterable[LogGroup], average_dr):
    print(f'writing log to {out_path}')
    with open(out_path, mode='x', encoding='utf8') as f:
        l1 = '-' * 80
        l2 = '=' * 80
        w = f.write
        w(f"generated by https://github.com/magicgoose/simple_dr_meter\n"
          f"log date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        for group in dr_log_groups:
            group_name = get_group_title(group)

            w(f"{l1}\nAnalyzed: {group_name}\n{l1}\n\nDR         Peak         RMS     Duration Track\n{l1}\n")
            track_count = 0
            for dr, peak, rms, duration_sec, track_name in group.tracks_dr:
                dr_formatted = f"DR{str(dr).ljust(4)}" if dr is not None else "N/A   "
                w(dr_formatted +
                  f"{peak:9.2f} dB"
                  f"{rms:9.2f} dB"
                  f"{format_time(duration_sec).rjust(10)} "
                  f"{track_name}\n")
                track_count += 1
            w(f"{l1}\n\nNumber of tracks:  {track_count}\nOfficial DR value: DR{average_dr}\n\n"
              f"Samplerate:        {group.sample_rate} Hz\nChannels:          {group.channels}\n{l2}\n\n")

    print('…done')


def flatmap(f, items):
    for i in items:
        yield from f(i)


def make_log_groups(l: Iterable[Tuple[AudioSourceInfo, Iterable[Tuple[int, float, float, int, str]]]]):
    import itertools
    grouped = itertools.groupby(l, key=lambda x: (x[0].channel_count, x[0].sample_rate))

    for ((channels, sample_rate), subitems) in grouped:
        subitems = tuple(subitems)
        performers = set(flatmap(lambda x: x[0].performers, subitems))
        albums = set(map(lambda x: x[0].album, subitems))
        tracks_dr = flatmap(lambda x: x[1], subitems)
        yield LogGroup(
            performers=performers,
            albums=albums,
            channels=channels,
            sample_rate=sample_rate,
            tracks_dr=tracks_dr)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help='Input file or directory')
    ap.add_argument("--no-log", help='Do not write log (dr.txt), by default a log file is written after analysis', action='store_true')
    ap.add_argument("--keep-precision", help='Do not round values, this also disables log', action='store_true')
    args = sys.argv[1:]
    if args:
        return ap.parse_args(args)
    else:
        ap.print_help()
        return None


def main():
    args = parse_args()
    if not args:
        return

    in_path = args.input
    should_write_log = not args.no_log and not args.keep_precision
    keep_precision = args.keep_precision

    if should_write_log:
        log_path = get_log_path(in_path)
        if os.path.exists(log_path):
            sys.exit('the log file already exists!')

    def track_cb(track_info, dr):
        dr_formatted = f'DR{dr}' if dr is not None else 'N/A'
        print(f"{track_info.global_index:02d} - {track_info.name}: {dr_formatted}")

    time_start = time.time()
    dr_log_items, dr_mean, dr_median = analyze_dr(in_path, track_cb, keep_precision)
    print(f'Official DR = {dr_mean}, Median DR = {dr_median}')
    print(f'Analyzed all tracks in {time.time() - time_start:.2f} seconds')

    if should_write_log:
        # noinspection PyUnboundLocalVariable
        write_log(log_path, dr_log_items, dr_mean)
    fix_tty()


def fix_tty():
    """I don't know why this is needed, but it is. Otherwise the terminal may cease to
    accept any keyboard input after this application finishes. Hopefully I will find
    something better eventually."""
    platform = sys.platform.lower()
    if platform.startswith('darwin') or platform.startswith('linux'):
        if os.isatty(sys.stdin.fileno()):
            os.system('stty sane')


def analyze_dr(in_path: str, track_cb, keep_precision: bool):
    audio_info = tuple(read_audio_info(in_path))
    num_files = len(audio_info)
    assert num_files > 0

    import multiprocessing.dummy as mt
    import multiprocessing

    cpu_count = multiprocessing.cpu_count()

    def choose_map_impl(threads, *, chunksize):
        if threads <= 1:
            return map
        pool = mt.Pool(threads)
        return functools.partial(pool.imap_unordered, chunksize=chunksize)

    threads_outer = max(1, min(num_files, cpu_count))
    threads_inner = cpu_count // threads_outer
    map_impl_outer = choose_map_impl(threads_outer, chunksize=1)
    map_impl_inner = choose_map_impl(threads_inner, chunksize=4)

    def analyze_part_tracks(audio_data: AudioSource, audio_info_part: AudioSourceInfo, map_impl):
        for track_samples, track_info in zip(audio_data.blocks_generator, audio_info_part.tracks):
            dr_metrics = compute_dr(map_impl, audio_info_part, track_samples, keep_precision)
            yield track_info, dr_metrics

    def analyze_part(map_impl, audio_info_part: AudioSourceInfo):
        audio_data = read_audio_data(audio_info_part, 3 * MEASURE_SAMPLE_RATE)
        return audio_info_part, analyze_part_tracks(audio_data, audio_info_part, map_impl)

    dr_items = []
    dr_log_items = []

    def process_results(audio_info_part, analyzed_tracks):
        nonlocal dr_items
        dr_log_subitems = []
        dr_log_items.append((audio_info_part, dr_log_subitems))
        track_results = []
        for track_info, dr_metrics in analyzed_tracks:
            dr = dr_metrics.dr
            track_results.append((track_info, dr))
            track_cb(track_info, dr)
            if dr:
                dr_items.append(dr)

            duration_seconds = round(dr_metrics.sample_count / MEASURE_SAMPLE_RATE)
            dr_log_subitems.append(
                (dr, dr_metrics.peak, dr_metrics.rms, duration_seconds,
                 f"{track_info.global_index:02d}-{track_info.name}"))
        return track_results

    def process_part(map_impl, audio_info_part: AudioSourceInfo):
        audio_info_part, analyzed_tracks = analyze_part(map_impl, audio_info_part)
        return process_results(audio_info_part, analyzed_tracks)

    for x in map_impl_outer(functools.partial(process_part, map_impl_inner), audio_info):
        for track_result in x:
            pass  # we need to go through all items for the side effects

    if keep_precision:
        dr_mean_rounded = numpy.mean(dr_items)
    else:
        dr_mean_rounded = int(numpy.round(numpy.mean(dr_items)))  # official
    dr_median = numpy.median(dr_items)

    dr_log_items = make_log_groups(dr_log_items)
    return dr_log_items, dr_mean_rounded, dr_median


if __name__ == '__main__':
    main()
