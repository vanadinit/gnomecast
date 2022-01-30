import os
import subprocess
import tempfile
import threading
import time
import traceback
from logging import getLogger

import ffmpeg
import pycaption

log = getLogger(f'gnomecast.{__name__}')


class Metadata:
    def __repr__(self):
        fields = [f'{k}:{v}' for k, v in self.__dict__.items() if v is not None and not k.startswith('_')]
        return '{class_name}({fields})'.format(
            class_name=self.__class__.__name__,
            fields=', '.join(fields)
        )


class StreamMetadata(Metadata):
    def __init__(self, index, codec, title=None):
        self.index = index
        self.codec = codec
        self.title = title


class AudioMetadata(StreamMetadata):
    def __init__(self, channels: int = 2, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channels = channels

    def details(self):
        if self.channels == 1:
            channels = 'mono'
        elif self.channels == 2:
            channels = 'stereo'
        elif self.channels == 6:
            channels = '5.1'
        elif self.channels == 8:
            channels = '7.1'
        else:
            channels = str(self.channels)
        return f'{self.title} ({self.codec}/{channels})'


class FileMetadata(Metadata):
    def __init__(self, fn, callback=None, _ffmpeg_output=None):
        self.fn = fn
        self.ready = False
        self.thumbnail_fn = None
        self.container = fn.lower().split(".")[-1]
        self.video_streams = []
        self.audio_streams = []
        self.subtitles = []
        self.ffoutput = ''

        def parse():
            self.create_thumbnail()
            self.ffprobe()
            self._important_ffmpeg = 'ffmpeg is now used in a different way'
            if not _ffmpeg_output:
                self.load_subtitles()
            self.ready = True
            if callback:
                callback(self)

        threading.Thread(target=parse).start()

    def create_thumbnail(self):
        thumbnail_fn = tempfile.mkstemp(suffix='.jpg', prefix=f'gnomecast_pid{os.getpid()}_thumbnail_')[1]
        os.remove(thumbnail_fn)
        ffmpeg.input(filename=self.fn, ss=27).filter('scale', 200, -1).output(thumbnail_fn, vframes=1).run()
        if os.path.isfile(thumbnail_fn):
            self.thumbnail_fn = thumbnail_fn

    def ffprobe(self):
        data = ffmpeg.probe(self.fn)
        self.ffoutput = data
        for stream in data['streams']:
            # First number refers to the input file, which is always 0, if we have just one
            index = '0:{}'.format(stream['index'])
            if stream.get('codec_type') == 'video':
                self.video_streams.append(StreamMetadata(
                    index=index,
                    codec=stream['codec_name'],
                    title=stream.get('tags', {}).get('language', f'Video #{len(self.video_streams) + 1}'),
                ))
            elif stream.get('codec_type') == 'audio':
                self.audio_streams.append(AudioMetadata(
                    index=index,
                    codec=stream['codec_name'],
                    title=stream.get('tags', {}).get('language', f'Audio #{len(self.audio_streams) + 1}'),
                    channels=stream['channels'],
                ))
            elif stream.get('codec_type') == 'subtitle':
                self.subtitles.append(StreamMetadata(
                    index=index,
                    codec=stream['codec_name'],
                    title=stream['tags']['language'],
                ))

    def wait(self):
        while not self.ready:
            time.sleep(1)

    def load_subtitles(self):
        if not self.subtitles:
            return
        cmd = f'ffmpeg -y -i {self.fn} -vn -an'
        streams_and_files = []
        for stream in self.subtitles:
            if stream.codec in ['dvdsub', 'pgssub', 'xsub']:
                # See
                # https://stackoverflow.com/questions/36326790/cant-change-video-subtitles-codec-using-ffmpeg
                # https://stackoverflow.com/questions/58808907/is-it-possible-to-determine-if-a-subtitle-track-is-imaged-based-or-text-based-wi
                print('Sorry, image based subtitles are not supported yet.')
                continue
            srt_fn = tempfile.mkstemp(suffix='.srt', prefix=f'gnomecast_pid{os.getpid()}_subtitles_')[1]
            streams_and_files.append((stream, srt_fn))
            cmd += f' -map {stream.index} -codec srt {srt_fn}'

        print(cmd)
        try:
            subprocess.check_output(cmd.split(' '), stderr=subprocess.STDOUT)
            for stream, srt_fn in streams_and_files:
                with open(srt_fn) as f:
                    caps = f.read()
                # print('caps', caps)
                converter = pycaption.CaptionConverter()
                converter.read(caps, pycaption.detect_format(caps)())
                stream._subtitles = converter.write(pycaption.WebVTTWriter())
                os.remove(srt_fn)
        except subprocess.CalledProcessError as exc:
            print('ERROR processing subtitles:', exc)
            traceback.print_tb(exc.__traceback__)
            self.subtitles = []

    def details(self):
        return \
            'File: {file}\n' \
            'Video: {video}\n' \
            'Audio: {audio}\n' \
            'Subtitles: {subtitles}\n'.format(
                file=os.path.basename(self.fn),
                video=', '.join([f'{vis.title} ({vis.codec})' for vis in self.video_streams]),
                audio=', '.join([aus.details() for aus in self.audio_streams]),
                subtitles=', '.join([s.title for s in self.subtitles]),
            )
