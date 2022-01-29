import os
import subprocess
import tempfile
import threading
import time

import pycaption


class StreamMetadata:

    def __init__(self, index, codec, title=None):
        self.index = index
        self.codec = codec
        self.title = title

    def __repr__(self):
        fields = ['%s:%s' % (k, v) for k, v in self.__dict__.items() if v is not None and not k.startswith('_')]
        return '%s(%s)' % (self.__class__.__name__, ', '.join(fields))


class AudioMetadata(StreamMetadata):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.channels = 2

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
        return '%s (%s/%s)' % (self.title, self.codec, channels)


class FileMetadata(object):
    def __init__(self, fn, callback=None, _ffmpeg_output=None):
        self.fn = fn
        self.ready = False

        def parse():
            self.thumbnail_fn = None
            thumbnail_fn = tempfile.mkstemp(suffix='.jpg', prefix='gnomecast_pid%i_thumbnail_' % os.getpid())[1]
            os.remove(thumbnail_fn)
            self._ffmpeg_output = _ffmpeg_output if _ffmpeg_output else subprocess.check_output(
                ['ffmpeg', '-i', fn, '-f', 'ffmetadata', '-', '-f', 'mjpeg', '-vframes', '1', '-ss', '27', '-vf',
                 'scale=200:-1', thumbnail_fn],
                stderr=subprocess.STDOUT
            ).decode()
            _important_ffmpeg = []
            if os.path.isfile(thumbnail_fn):
                self.thumbnail_fn = thumbnail_fn
            output = self._ffmpeg_output.split('\n')
            self.container = fn.lower().split(".")[-1]
            self.video_streams = []
            self.audio_streams = []
            self.subtitles = []
            stream = None
            for line in output:
                line = line.strip()
                if line.startswith('ffmpeg version'):
                    _important_ffmpeg.append(line)
                if line.startswith('Stream') and 'Video' in line:
                    _important_ffmpeg.append(line)
                    id = line.split()[1].strip('#').strip(':')
                    title = 'Video #%i' % (len(self.video_streams) + 1)
                    if '(' in id:
                        title = id[id.index('(') + 1:id.index(')')]
                        id = id[:id.index('(')]
                    video_codec = line.split()[3]
                    stream = StreamMetadata(id, video_codec, title=title)
                    self.video_streams.append(stream)
                elif line.startswith('Stream') and 'Audio' in line:
                    _important_ffmpeg.append(line)
                    title = 'Audio #%i' % (len(self.audio_streams) + 1)
                    id = line.split()[1].strip('#').strip(':')
                    if '(' in id:
                        title = id[id.index('(') + 1:id.index(')')]
                        id = id[:id.index('(')]
                    audio_codec = line.split()[3].strip(',')
                    stream = AudioMetadata(id, audio_codec, title=title)
                    if ', stereo, ' in line: stream.channels = 1
                    if ', stereo, ' in line: stream.channels = 2
                    if ', 5.1' in line: stream.channels = 6
                    if ', 7.1' in line: stream.channels = 8
                    self.audio_streams.append(stream)
                elif line.startswith('Stream') and 'Subtitle' in line:
                    _important_ffmpeg.append(line)
                    id = line.split()[1].strip('#').strip(':')
                    print(line, id)
                    if '(' in id:
                        title = id[id.index('(') + 1:id.index(')')]
                        id = id[:id.index('(')]
                    stream = StreamMetadata(id, None, title=title)
                    self.subtitles.append(stream)
                elif stream and line.startswith('title'):
                    _important_ffmpeg.append(line)
                    stream.title = line.split()[2]
                elif line.startswith('Output'):
                    break
            self._important_ffmpeg = '\n'.join(_important_ffmpeg)
            if not _ffmpeg_output:
                self.load_subtitles()
            self.ready = True
            if callback: callback(self)

        threading.Thread(target=parse).start()

    def wait(self):
        while not self.ready:
            time.sleep(1)

    def load_subtitles(self):
        if not self.subtitles: return
        cmd = ['ffmpeg', '-y', '-i', self.fn, '-vn', '-an', ]
        files = []
        for stream in self.subtitles:
            srt_fn = tempfile.mkstemp(suffix='.srt', prefix='gnomecast_pid%i_subtitles_' % os.getpid())[1]
            files.append(srt_fn)
            cmd += ['-map', stream.index, '-codec', 'srt', srt_fn]

        print(cmd)
        try:
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
            for stream, srt_fn in zip(self.subtitles, files):
                with open(srt_fn) as f:
                    caps = f.read()
                # print('caps', caps)
                converter = pycaption.CaptionConverter()
                converter.read(caps, pycaption.detect_format(caps)())
                stream._subtitles = converter.write(pycaption.WebVTTWriter())
                os.remove(srt_fn)
        except subprocess.CalledProcessError as e:
            print('ERROR processing subtitles:', e)
            self.subtitles = []

    def __repr__(self):
        fields = ['%s:%s' % (k, v) for k, v in self.__dict__.items() if not k.startswith('_')]
        return 'FileMetadata(%s)' % ', '.join(fields)

    def details(self):
        fields = [
            'File: %s' % os.path.basename(self.fn),
            'Video: %s' % ', '.join(['%s (%s)' % (s.title, s.codec) for s in self.video_streams]),
            'Audio: %s' % ', '.join([s.details() for s in self.audio_streams]),
            'Subtitles: %s' % ', '.join([s.title for s in self.subtitles]),
        ]
        return '\n'.join(fields)
