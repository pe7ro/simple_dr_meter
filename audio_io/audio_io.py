import re
import subprocess as sp

import sys
from subprocess import DEVNULL, PIPE
from typing import NamedTuple, Iterator

import numpy as np

ex_ffprobe = 'ffprobe'
ex_ffmpeg = 'ffmpeg'


class AudioSourceInfo(NamedTuple):
    path: str
    channel_count: int
    sample_rate: int


class AudioSource(NamedTuple):
    source_info: AudioSourceInfo
    samples_per_block: int
    blocks_generator: Iterator[np.ndarray]


def read_audio_info(in_path: str) -> AudioSourceInfo:
    channel_count, sample_rate = _get_params(in_path)
    if channel_count < 1 or sample_rate < 8000:
        sys.exit('invalid format: channels={}, sample_rate={}'.format(channel_count, sample_rate))
    # noinspection PyArgumentList
    return AudioSourceInfo(in_path, channel_count, sample_rate)


def read_audio_data(what: AudioSourceInfo, samples_per_block: int) -> AudioSource:
    audio_blocks = _read_audio_blocks(what.path, what.channel_count, samples_per_block)
    # noinspection PyArgumentList
    return AudioSource(what, samples_per_block, audio_blocks)


def _test_ffmpeg():
    try:
        for n in (ex_ffmpeg, ex_ffprobe):
            sp.check_call((n, '-version'), stderr=DEVNULL, stdout=DEVNULL)
    except sp.CalledProcessError:
        sys.exit('ffmpeg not installed, broken or not on PATH')


def _parse_audio_params(s):
    d = {}
    for m in re.finditer(r'([a-z_]+)=([0-9]+)', s):
        v = m.groups()
        d.update({v[0]: int(v[1])})

    def values(channels, sample_rate):
        return channels, sample_rate

    return values(**d)


def _get_params(in_path):
    p = sp.Popen(
        (ex_ffprobe,
         '-v', 'error',
         '-select_streams', '0:a:0',
         '-show_entries', 'stream=channels,sample_rate',
         in_path),
        stdout=PIPE)
    out, err = p.communicate()
    returncode = p.returncode
    if returncode != 0:
        raise Exception('ffprobe returned {}'.format(returncode))
    out = out.decode('utf-8')
    return _parse_audio_params(out)


def _read_audio_blocks(in_path, channel_count, block_samples):
    bytes_per_block = 4 * channel_count * block_samples
    p = sp.Popen(
        (ex_ffmpeg, '-v', 'error',
         '-i', in_path,
         '-map', '0:a:0',
         '-c:a', 'pcm_f32le',
         '-f', 'f32le',
         '-'),
        stderr=None,
        stdout=PIPE)

    with p.stdout as f:
        readinto = type(f).readinto
        buffer = bytearray(bytes_per_block)
        frombuffer = np.frombuffer
        reshape = np.reshape

        sample_type = np.dtype('<f4')

        while True:
            read_size = readinto(f, buffer)
            if not read_size:
                break

            a = frombuffer(buffer, dtype=sample_type, count=read_size // 4)
            a = reshape(a, (channel_count, -1), order='F')
            yield a
