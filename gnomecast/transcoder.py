import os
import re
import subprocess
import tempfile
import threading
import time
import traceback


class Device:
    def __init__(self, h265=None, ac3=None):
        self.h265 = h265
        self.ac3 = ac3


HARDWARE = {
    ('Unknown manufacturer', 'Chromecast'): Device(h265=False, ac3=False),
    ('Google Inc.', 'Chromecast'): Device(h265=False, ac3=False),
    ('Unknown manufacturer', 'Chromecast Ultra'): Device(h265=True, ac3=True),
    ('Unknown manufacturer', 'Google Home Mini'): Device(h265=False, ac3=False),
    ('Unknown manufacturer', 'Google Home'): Device(h265=False, ac3=False),
    ('VIZIO', 'P75-F1'): Device(h265=True, ac3=True),
}


class Transcoder(object):
    def __init__(self, cast, fmd, video_stream, audio_stream, done_callback, error_callback, prev_transcoder=None,
                 force_audio=False, force_video=False, fake=False):
        self.fmd = fmd
        self.video_stream = video_stream
        self.audio_stream = audio_stream
        fn = fmd.fn
        self.cast = cast
        self.source_fn = fn
        self.p = None

        if prev_transcoder:
            prev_transcoder.destroy()

        print('Transcoder', fn)
        # As far as I discovered the container format is not significant for the transcoding decision.
        # transcode_container = fmd.container not in ('mp4', 'aac', 'mp3', 'wav')
        self.transcode_video = force_video or self.video_needs_transcode(self.video_stream)
        self.transcode_audio = force_audio or self.audio_needs_transcode(self.audio_stream)
        self.transcode = self.transcode_video or self.transcode_audio
        self.trans_fn = None

        self.progress_bytes = 0
        self.progress_seconds = 0
        self.done_callback = done_callback
        self.error_callback = error_callback
        print('transcode, transcode_video, transcode_audio', self.transcode, self.transcode_video, self.transcode_audio)

        # Uncomment next line to test different formats without transcoding
        # self.transcode = False
        #
        # See also https://developers.google.com/cast/docs/media
        # Test results:
        # extension / video codec / audio codec
        # --------------------------
        # working:
        # - avi  / h264  / aac
        # - avi  / h264  / mp3
        # - mkv  / h264  / vorbis
        # - webm / vp8   / vorbis
        # --------------------------
        # failing:
        # - avi  / msmpeg4v3  / mp3
        # - avi  / msmpeg4v3  / mp3 (only audio works)
        # - avi  / mpeg4      / ac3
        # - avi  / mpeg4      / mp3 (only audio works)
        # - avi  / rawvideo   / pcm_s16le
        # - mkv  / h264       / ac3 (only video works)
        # - mp4  / hevc       / aac (only audio works)
        # - mp4  / mpeg4      / aac (only audio works)
        # - mpg  / mpeg1video / mp2

        if self.transcode:
            self.done = False
            dir = '/var/tmp' if os.path.isdir('/var/tmp') else None
            self.trans_fn = tempfile.mkstemp(
                suffix='.mp4', prefix='gnomecast_pid%i_transcode_' % os.getpid(), dir=dir)[1]
            os.remove(self.trans_fn)

            device_info = HARDWARE.get((self.cast.cast_info.manufacturer, self.cast.model_name))
            ac3 = device_info.ac3 if device_info else None
            transcode_audio_to = 'ac3' if (ac3 or ac3 is None) and audio_stream and audio_stream.channels > 2 else 'mp3'

            self.transcode_cmd = ['ffmpeg', '-i', self.source_fn, '-map', self.video_stream.index]
            if self.audio_stream:
                self.transcode_cmd += [
                    '-map', self.audio_stream.index,
                    '-c:a', transcode_audio_to if self.transcode_audio else 'copy'
                ]
                if self.transcode_audio:
                    self.transcode_cmd += ['-b:a', '256k']
            self.transcode_cmd += ['-c:v', 'h264' if self.transcode_video else 'copy']  # '-movflags', 'faststart'
            self.transcode_cmd += [self.trans_fn]
            print(self.transcode_cmd)
            print(' '.join(["'%s'" % s if ' ' in s else s for s in self.transcode_cmd]))
            if fake:
                self.p = None
                self.monitor()
            else:
                print('---------------------')
                print(' starting ffmpeg at:')
                print('---------------------')
                traceback.print_stack()
                self.p = subprocess.Popen(self.transcode_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                t = threading.Thread(target=self.monitor)
                t.daemon = True
                t.start()
        else:
            self.done = True
            self.done_callback()

    @property
    def fn(self):
        return self.trans_fn if self.transcode else self.source_fn

    def video_needs_transcode(self, video_stream):
        try:
            video_codec = video_stream.codec
        except Exception:
            print('No valid video stream provided. No need to transcode video.')
            return False
        if self.cast.cast_type == 'audio':
            print('Cast type is audio. No need to transcode video.')
            return False
        if video_codec in ['h264', 'vp8']:
            return False
        if video_codec in ['h265', 'hevc']:
            device_info = HARDWARE.get((self.cast.cast_info.manufacturer, self.cast.model_name))
            if device_info and device_info.h265 is not None:
                return device_info.h265 is False
        return True

    def audio_needs_transcode(self, audio_stream):
        try:
            audio_codec = audio_stream.codec
        except:
            print('No valid audio stream provided. No need to transcode audio.')
            return False
        if audio_codec in ['aac', 'mp3', 'vorbis']:
            return False
        if audio_codec in ['ac3']:
            device_info = HARDWARE.get((self.cast.cast_info.manufacturer, self.cast.model_name))
            if device_info and device_info.ac3 is not None:
                return device_info.ac3 is False
        return True

    def wait_for_byte(self, offset, buffer=128 * 1024 * 1024):
        if self.done:
            return
        if self.source_fn.lower().split(".")[-1] == 'mp4':
            while offset > self.progress_bytes + buffer:
                print('waiting for', offset, 'at', self.progress_bytes + buffer)
                time.sleep(2)
        else:
            while not self.done:
                print('waiting for transcode to finish')
                time.sleep(2)
        print('done waiting')

    def monitor(self):
        line = b''
        r = re.compile(r'=\s+')
        total_output = b''
        while self.p:
            byte = self.p.stdout.read(1)
            total_output += byte
            if byte == b'' and self.p.poll() != None:
                break
            if byte != b'':
                line += byte
                if byte == b'\r':
                    # frame=92578 fps=3937 q=-1.0 size= 1142542kB time=01:04:21.14 bitrate=2424.1kbits/s speed= 164x
                    line = line.decode()
                    line = r.sub('=', line)
                    items = [s.split('=') for s in line.split()]
                    d = dict([x for x in items if len(x) == 2])
                    print(d)
                    self.progress_bytes = int(d.get('size', '0kb')[:-2]) * 1024
                    self.progress_seconds = parse_ffmpeg_time(d.get('time', '00:00:00'))
                    line = b''
        if self.p:
            self.p.stdout.close()
            if self.p.returncode:
                print('--== transcode error ==--')
                print(total_output)
                self.error_callback(total_output.decode())
                return
        self.done = True
        if self.done_callback:
            self.done_callback(did_transcode=True)

    def destroy(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
        if self.trans_fn and os.path.isfile(self.trans_fn):
            os.remove(self.trans_fn)

    def __del__(self):
        self.destroy()


def parse_ffmpeg_time(time_s):
    """
    Converts ffmpeg's time string to number of seconds
    :param time_s:
    :return: number of seconds
    """
    hours, minutes, seconds = (float(s) for s in time_s.split(':'))
    return hours * 60 * 60 + minutes * 60 + seconds
