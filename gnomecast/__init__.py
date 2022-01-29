import contextlib
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib

from gnomecast.metadata import StreamMetadata, FileMetadata
from gnomecast.transcoder import Transcoder, parse_ffmpeg_time

# TODO
#  - arg_parse
#  - refactoring: split in multiple files, functions, ...
#  - new feature: controls via chromecast? play, pause, stop

DEPS_MET = True
try:
    import pychromecast
    import bottle
    import html5lib.treebuilders
    import pycaption
except Exception as e:
    traceback.print_exc()
    print(e)
    DEPS_MET = False

DBUS_AVAILABLE = False
try:
    import dbus

    DBUS_AVAILABLE = True
except Exception as e:
    print(e)

try:
    import gi

    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, Gdk, GLib, GdkPixbuf, Gio
except ImportError:
    line = "-" * 70
    ERROR_MESSAGE = """
{}
Python package "gi" (for building the GU not found.\n
If on Debian or Ubuntu, please run:
$ sudo apt-get install python3-gi\n
For other distributions please look up the equivalent package.\n
If this doesn't work, please report the error here:
https://github.com/keredson/gnomecast\n
Thanks! - Gnomecast
{}
"""
    print(ERROR_MESSAGE.format(line, line))
    sys.exit(1)

__version__ = '1.9.11'

if DEPS_MET:
    pycaption.WebVTTWriter._encode = lambda self, s: s


def throttle(seconds=2):
    def decorator(f):
        timer = None
        lastest_args, latest_kwargs = None, None

        def run_f():
            nonlocal timer, lastest_args, latest_kwargs
            ret = f(*lastest_args, **latest_kwargs)
            timer = None
            return ret

        def wrapper(*args, **kwargs):
            nonlocal timer, lastest_args, latest_kwargs
            lastest_args, latest_kwargs = args, kwargs
            if timer is None:
                timer = threading.Timer(seconds, run_f)
                timer.start()

        return wrapper

    return decorator


def find_screensaver_dbus_iface(bus):
    """ Searches the DBus names for Screensaver and returns correct Interface"""
    if not DBUS_AVAILABLE: return None
    for path, name in [('org.freedesktop.ScreenSaver', '/ScreenSaver'), ('org.mate.ScreenSaver', '/ScreenSaver')]:
        try:
            saver = bus.get_object(path, name)
            return dbus.Interface(saver, dbus_interface=path)
        except dbus.exceptions.DBusException as e:
            # wrong path, try next one
            print(e)
    return None


AUDIO_EXTS = ('aac', 'mp3', 'wav')


class Gnomecast(object):
    def __init__(self):
        self.ip = (([ip for ip in socket.gethostbyname_ex(socket.gethostname())[2] if not ip.startswith("127.")] or [
            [(s.connect(("8.8.8.8", 53)), s.getsockname()[0], s.close()) for s in
             [socket.socket(socket.AF_INET, socket.SOCK_DGRAM)]][0][1]]) + [None])[0]
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.bind(('0.0.0.0', 0))
            self.port = s.getsockname()[1]
        self.app = bottle.Bottle()
        self.cast = None
        self.last_known_player_state = None
        self.last_known_current_time = None
        self.last_time_current_time = None
        self.fn = None
        self.video_stream = None
        self.audio_stream = None
        self.last_fn_played = None
        self.transcoder = None
        self.duration = None
        self.subtitles = None
        self.seeking = False
        self.last_known_volume_level = None
        bus = dbus.SessionBus() if DBUS_AVAILABLE else None
        self.saver_interface = find_screensaver_dbus_iface(bus)
        self.inhibit_screensaver_cookie = None
        self.autoplay = False

    def run(self, fn=None, device=None, subtitles=None):
        self.build_gui()
        self.init_casts(device=device)
        threading.Thread(target=self.check_ffmpeg).start()
        t = threading.Thread(target=self.start_server)
        t.daemon = True
        t.start()
        t = threading.Thread(target=self.monitor_cast)
        t.daemon = True
        t.start()
        if fn:
            self.queue_files([fn])
        if subtitles:
            self.select_subtitles_file(subtitles)
        if fn and subtitles:
            self.autoplay = True
        Gtk.main()

    def check_ffmpeg(self):
        time.sleep(1)
        ffmpeg_available = True
        print('check_ffmpeg')
        try:
            print(subprocess.check_output(['which', 'ffmpeg']))
        except Exception as e:
            print(e, e.output)
            ffmpeg_available = False
        if not ffmpeg_available:
            def f():
                dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE,
                                           "FFMPEG not Found")
                dialog.format_secondary_text("Could not find ffmpeg.  Please run 'sudo apt-get install ffmpeg'.")
                dialog.run()
                dialog.destroy()
                # TODO: there's a weird pause here closing the dialog.  why?
                sys.exit(1)

            GLib.idle_add(f)

    def start_server(self):
        app = self.app

        @app.route('/subtitles.vtt')
        def subtitles():
            # response = bottle.static_file(self.subtitles_fn, root='/', mimetype='text/vtt')
            response = bottle.response
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            response.headers['Content-Type'] = 'text/vtt'
            return self.subtitles

        @app.get('/media/<id>.<ext>')
        def video(id, ext):
            print(list(bottle.request.headers.items()))
            ranges = list(bottle.parse_range_header(bottle.request.environ['HTTP_RANGE'], 1000000000000))
            print('ranges', ranges)
            offset, end = ranges[0]
            self.transcoder.wait_for_byte(offset)
            response = bottle.static_file(self.transcoder.fn, root='/')
            if 'Last-Modified' in response.headers:
                del response.headers['Last-Modified']
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'GET, HEAD'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
            return response

        # app.run(host=self.ip, port=self.port, server='paste', daemon=True)
        from paste import httpserver
        from paste.translogger import TransLogger
        handler = TransLogger(app, setup_console_handler=True)
        httpserver.serve(handler, host=self.ip, port=str(self.port), daemon_threads=True)

    def update_status(self, did_transcode=False):
        if did_transcode:
            self.update_button_visible()
            self.prep_next_transcode()

        #    if self.last_known_player_state and self.last_known_player_state!='UNKNOWN':
        #      notes.append('Cast: %s' % self.last_known_player_state)
        def f():
            for row in self.files_store:
                duration = row[2]
                transcoder = row[7]
                if transcoder:
                    if duration:
                        if transcoder.done:
                            row[5] = 100
                        else:
                            row[5] = transcoder.progress_seconds * 100 // duration

        GLib.idle_add(f)

    def monitor_cast(self):
        while True:
            time.sleep(1)
            if not self.cast: continue
            seeking = self.seeking
            cast = self.cast
            mc = cast.media_controller
            if mc.status.player_state != self.last_known_player_state:
                if mc.status.player_state == 'PLAYING' and self.last_known_player_state == 'BUFFERING' and seeking:
                    self.seeking = False
                if mc.status.player_state == 'IDLE' and self.last_known_player_state == 'PLAYING':
                    self.check_for_next_in_queue()
                if mc.status.player_state == 'PLAYING':
                    self.inhibit_screensaver()
                else:
                    self.restore_screensaver()
                self.last_known_player_state = mc.status.player_state

                def f():
                    self.update_media_button_states()
                    self.update_status()

                GLib.idle_add(f)
            elif self.transcoder and not self.transcoder.done:
                def f():
                    self.update_status()

                GLib.idle_add(f)
            if self.last_known_current_time != mc.status.current_time:
                self.last_known_current_time = mc.status.current_time
                self.last_time_current_time = time.time()
            if not seeking and mc.status.player_state == 'PLAYING':
                GLib.idle_add(
                    lambda: self.scrubber_adj.set_value(
                        mc.status.current_time + time.time() - self.last_time_current_time))

    def init_casts(self, widget=None, device=None):
        self.cast_store.clear()
        self.cast_store.append([None, "Searching local network - please wait..."])
        self.cast_combo.set_active(0)
        threading.Thread(target=self.load_casts, kwargs={'device': device}).start()

    def inhibit_screensaver(self):
        if not self.saver_interface or self.inhibit_screensaver_cookie: return
        self.inhibit_screensaver_cookie = self.saver_interface.Inhibit("Gnomecast", "Player is playing...")
        print('disabled screensaver')

    def restore_screensaver(self):
        if self.saver_interface and self.inhibit_screensaver_cookie:
            self.saver_interface.UnInhibit(self.inhibit_screensaver_cookie)
            self.inhibit_screensaver_cookie = None
            print('restored screensaver')

    def load_casts(self, device=None):
        chromecasts = pychromecast.get_chromecasts()
        # workaround for https://github.com/home-assistant-libs/pychromecast/issues/398
        if isinstance(chromecasts, tuple) and len(chromecasts) == 2:
            chromecasts = chromecasts[0]

        def f():
            self.cast_store.clear()
            self.cast_store.append([None, "Select a cast device..."])
            self.cast_store.append([-1, 'Add a non-local Chromecast...'])
            for cc in chromecasts:
                friendly_name = cc.name
                if cc.cast_type != 'cast':
                    friendly_name = '%s (%s)' % (friendly_name, cc.cast_type)
                self.cast_store.append([cc, friendly_name])
            if device:
                found = False
                for i, cc in enumerate(chromecasts):
                    if device == cc.name:
                        self.cast_combo.set_active(i + 1)
                        found = True
                if not found:
                    self.cast_combo.set_active(0)
                    dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE,
                                               "Chromecast Not Found")
                    dialog.format_secondary_text("The Chromecast '%s' wasn't found." % device)
                    dialog.run()
                    dialog.destroy()
            else:
                self.cast_combo.set_active(2 if len(chromecasts) == 1 else 0)

        GLib.idle_add(f)

    def update_media_button_states(self):
        mc = self.cast.media_controller if self.cast else None
        self.play_button.set_sensitive(bool(self.transcoder and self.cast and mc.status.player_state in (
            'BUFFERING', 'PLAYING', 'PAUSED', 'IDLE', 'UNKNOWN') and self.fn))
        self.volume_button.set_sensitive(bool(self.cast))
        self.stop_button.set_sensitive(
            bool(self.transcoder and self.cast and mc.status.player_state in ('BUFFERING', 'PLAYING', 'PAUSED')))
        self.rewind_button.set_sensitive(
            bool(self.transcoder and self.cast and mc.status.player_state in ('BUFFERING', 'PLAYING', 'PAUSED')))
        self.forward_button.set_sensitive(
            bool(self.transcoder and self.cast and mc.status.player_state in ('BUFFERING', 'PLAYING', 'PAUSED')))
        self.play_button.set_image(
            Gtk.Image(stock=Gtk.STOCK_MEDIA_PAUSE) if self.cast and mc.status.player_state == 'PLAYING' else Gtk.Image(
                stock=Gtk.STOCK_MEDIA_PLAY))
        if self.transcoder and self.duration:
            self.scrubber_adj.set_upper(self.duration)
            self.scrubber.set_sensitive(True)
        else:
            self.scrubber.set_sensitive(False)
        self.update_button_visible()

    def build_gui(self):
        self.win = win = Gtk.ApplicationWindow(title='Gnomecast v%s' % __version__)
        win.set_border_width(0)
        win.set_icon(self.get_logo_pixbuf(color='#000000'))
        enforce_target = Gtk.TargetEntry.new('text/plain', Gtk.TargetFlags(4), 129)
        win.drag_dest_set(Gtk.DestDefaults.ALL, [enforce_target], Gdk.DragAction.COPY)
        win.connect("drag-data-received", self.on_drag_data_received)
        self.cast_store = cast_store = Gtk.ListStore(object, str)

        vbox_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        self.thumbnail_image = Gtk.Image()
        self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
        vbox_outer.pack_start(self.thumbnail_image, True, False, 0)
        alignment = Gtk.Alignment(xscale=1, yscale=1)
        alignment.add(vbox)
        alignment.set_padding(16, 20, 16, 16)
        vbox_outer.pack_start(alignment, False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(hbox, False, False, 0)
        self.cast_combo = cast_combo = Gtk.ComboBox.new_with_model(cast_store)
        cast_combo.set_entry_text_column(1)
        renderer_text = Gtk.CellRendererText()
        cast_combo.pack_start(renderer_text, True)
        cast_combo.add_attribute(renderer_text, "text", 1)
        hbox.pack_start(cast_combo, True, True, 0)
        refresh_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_REFRESH))
        refresh_button.connect("clicked", self.init_casts)
        hbox.pack_start(refresh_button, False, False, 0)

        win.add(vbox_outer)

        # list of queued files
        # name, path, duration, duration_str, thumbnail_fn, transcode_progress, status_icon, transcoder, file_metadata
        self.files_store = Gtk.ListStore(str, str, int, str, str, int, str, object, object)
        self.files_store.connect("row-inserted", self.update_button_visible)
        self.files_store.connect("row-deleted", self.update_button_visible)
        self.files_view = Gtk.TreeView(self.files_store)
        self.files_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.files_view.set_headers_visible(False)
        self.files_view.set_rules_hint(True)
        column = Gtk.TreeViewColumn("Name", Gtk.CellRendererText(), text=0)
        column.set_expand(True)
        self.files_view.append_column(column)
        self.file_view_column_renderer = r = Gtk.CellRendererText()
        r.props.xalign = 1.0
        self.files_view.append_column(Gtk.TreeViewColumn("Duration", r, text=3))
        self.files_view_progress_column = column_progress = Gtk.TreeViewColumn("Progress", Gtk.CellRendererProgress(),
                                                                               value=5)
        self.files_view.append_column(column_progress)

        column_pixbuf = Gtk.TreeViewColumn("Playing", Gtk.CellRendererPixbuf(), icon_name=6)
        self.files_view.append_column(column_pixbuf)

        select = self.files_view.get_selection()
        select.connect("changed", self.on_files_view_selection_changed)
        self.files_view.connect("row-activated", self.on_files_view_row_activated)

        # contains the files list and the buttons to add/del
        self.hbox = hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(hbox, False, False, 0)

        self.scrolled_window = Gtk.ScrolledWindow()
        self.scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scrolled_window.add(self.files_view)
        hbox.pack_start(self.scrolled_window, True, True, 0)

        self.btn_vbox = btn_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        hbox.pack_start(btn_vbox, True, True, 0)
        self.file_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_ADD))
        self.file_button.set_tooltip_text('Add one or more audio or video files...')
        self.file_button.set_always_show_image(True)
        self.file_button.connect("clicked", self.on_file_clicked)
        btn_vbox.pack_start(self.file_button, True, True, 0)
        self.remove_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_REMOVE))
        self.remove_button.set_tooltip_text('Overwrite original file with transcoded version.')
        self.remove_button.connect("clicked", self.remove_files)
        self.remove_button.set_sensitive(False)
        btn_vbox.pack_start(self.remove_button, False, False, 0)

        self.file_detail_row = hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        vbox.pack_start(self.file_detail_row, False, False, 0)

        # audio/video track selection
        self.stream_store = Gtk.ListStore(str, object, object)
        self.audio_combo = Gtk.ComboBox.new_with_model(self.stream_store)
        self.audio_combo.connect("changed", self.on_audio_combo_changed)
        self.audio_combo.set_entry_text_column(0)
        renderer_text = Gtk.CellRendererText()
        self.audio_combo.pack_start(renderer_text, True)
        self.audio_combo.add_attribute(renderer_text, "text", 0)
        self.file_detail_row.pack_start(self.audio_combo, True, True, 0)

        # subtitle selection
        self.subtitle_store = Gtk.ListStore(str, object, object)  # title, stream, callback
        self.subtitle_combo = Gtk.ComboBox.new_with_model(self.subtitle_store)
        self.subtitle_combo.connect("changed", self.on_subtitle_combo_changed)
        self.subtitle_combo.set_entry_text_column(0)
        renderer_text = Gtk.CellRendererText()
        self.subtitle_combo.pack_start(renderer_text, True)
        self.subtitle_combo.add_attribute(renderer_text, "text", 0)
        self.subtitle_combo.set_active(0)
        self.file_detail_row.pack_start(self.subtitle_combo, True, True, 0)

        file_info_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_DIALOG_INFO))
        file_info_button.connect("clicked", self.show_file_info)
        self.file_detail_row.pack_start(file_info_button, False, False, 0)

        self.scrubber_adj = Gtk.Adjustment(0, 0, 100, 15, 60, 0)
        self.scrubber = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.scrubber_adj)
        self.scrubber.set_digits(0)

        def f(scale, s):
            notes = [self.humanize_seconds(s)]
            return ''.join(notes)

        self.scrubber.connect("format-value", f)
        self.scrubber.connect("change-value", self.scrubber_move_started)
        self.scrubber.connect("change-value", self.scrubber_moved)
        self.scrubber.set_sensitive(False)
        vbox.pack_start(self.scrubber, False, False, 0)

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self.rewind_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_MEDIA_REWIND))
        self.rewind_button.connect("clicked", self.rewind_clicked)
        self.rewind_button.set_sensitive(False)
        self.rewind_button.set_relief(Gtk.ReliefStyle.NONE)
        hbox.pack_start(self.rewind_button, True, False, 0)
        self.play_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_MEDIA_PLAY))
        self.play_button.connect("clicked", self.play_clicked)
        self.play_button.set_sensitive(False)
        self.play_button.set_relief(Gtk.ReliefStyle.NONE)
        hbox.pack_start(self.play_button, True, False, 0)
        self.forward_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_MEDIA_FORWARD))
        self.forward_button.connect("clicked", self.forward_clicked)
        self.forward_button.set_sensitive(False)
        self.forward_button.set_relief(Gtk.ReliefStyle.NONE)
        hbox.pack_start(self.forward_button, True, False, 0)
        self.stop_button = Gtk.Button(None, image=Gtk.Image(stock=Gtk.STOCK_MEDIA_STOP))
        self.stop_button.connect("clicked", self.stop_clicked)
        self.stop_button.set_sensitive(False)
        self.stop_button.set_relief(Gtk.ReliefStyle.NONE)
        hbox.pack_start(self.stop_button, True, False, 0)
        self.volume_button = Gtk.VolumeButton()
        self.volume_button.set_value(1)
        self.volume_button.connect("value-changed", self.volume_moved)
        self.volume_button.set_sensitive(False)
        hbox.pack_start(self.volume_button, True, False, 0)
        vbox.pack_start(hbox, False, False, 0)

        cast_combo.connect("changed", self.on_cast_combo_changed)

        win.connect("delete-event", self.quit)
        win.connect("key_press_event", self.on_key_press)
        win.show_all()

        self.update_button_visible()

        win.resize(1, 1)

        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.quit)

    def add_extra_subtitle_options(self):
        self.subtitle_store.prepend(["No subtitles.", None, None])
        self.subtitle_store.append(["Add subtitle file...", None, self.on_new_subtitle_clicked])
        # self.subtitle_store.append(["Download...", None, self.on_download_subtitle_clicked])
        self.subtitle_combo.set_active(0)

    def on_drag_data_received(self, widget, drag_context, x, y, data, info, time):
        fn = data.get_text()
        if fn.startswith('file://'):
            fn = urllib.parse.unquote(fn[len('file://'):]).strip()
            self.queue_files([fn])

    def update_button_visible(self, x=None, y=None, z=None):
        print('update_button_visible')
        count = len(self.files_store)
        self.scrolled_window.set_visible(count)
        self.remove_button.set_visible(count)
        self.file_button.set_label('' if count else '  Add one or more audio or video files...')
        self.file_button.get_child().set_padding(1, 0, 2, 0)  # w/ an empty label the + icon isn't quite centered
        self.hbox.set_child_packing(self.btn_vbox, not count, not count, 0, Gtk.PackType.START)
        self.file_detail_row.set_visible(bool(self.fn))

    def scrubber_move_started(self, scale, scroll_type, seconds):
        print('scrubber_move_started', seconds)
        self.seeking = True

    def on_files_view_selection_changed(self, selection):
        model, treeiter = selection.get_selected_rows()
        self.remove_button.set_sensitive(bool(treeiter))

    def remove_files(self, w):
        store, paths = self.files_view.get_selection().get_selected_rows()
        for path in reversed(paths):
            print('remove', path)
            iterx = store.get_iter(path)
            transcoder = store.get_value(iterx, 7)
            if transcoder:
                transcoder.destroy()
            fn = store.get_value(iterx, 1)
            store.remove(iterx)
            if self.fn == fn:
                self.unselect_file()

    def on_files_view_row_activated(self, widget, row, col):
        model = widget.get_model()
        print('double-clicked', model[row][:])
        fn = model[row][1]
        self.unselect_file()
        self.fn = fn
        self.transcoder = model[row][7]
        self.duration = model[row][2]
        thumbnail_fn = model[row][4]
        if thumbnail_fn and os.path.isfile(thumbnail_fn):
            self.thumbnail_image.set_from_file(thumbnail_fn)
        if self.cast:
            self.cast.media_controller.stop()

        def f():
            self.win.resize(1, 1)
            self.scrubber_adj.set_value(0)
            for row in self.files_store:
                if self.fn == row[1]:
                    row[6] = 'video-x-generic'
                else:
                    row[6] = None
            self.update_button_visible()
            self.update_media_button_states()

        GLib.idle_add(f)

        return True

    def queue_files(self, files):
        existing_files = set([row[1] for row in self.files_store])
        files = [f for f in files if f not in existing_files]
        for fn in files:
            display = os.path.basename(fn)
            MAX_LEN = 40
            if len(display) > MAX_LEN:
                display = display[:MAX_LEN - 10] + '...' + display[-10:]

            def callback(fmd):
                print(fmd)
                if os.path.isfile(fmd.thumbnail_fn):
                    for row in self.files_store:
                        if row[1] == fmd.fn:
                            row[4] = fmd.thumbnail_fn

                def f():
                    if self.fn == fmd.fn and fmd.thumbnail_fn:
                        self.thumbnail_image.set_from_file(fmd.thumbnail_fn)
                        self.win.resize(1, 1)
                    self.update_status()

                GLib.idle_add(f)

            fmd = FileMetadata(fn, callback)
            self.files_store.append([display, fn, None, '...', None, None, None, None, fmd])
            threading.Thread(target=self.get_info, args=[fn]).start()
        self.scrolled_window.set_visible(True)
        if len(files) and self.fn is None:
            self.select_file(files[0])
        path = Gtk.TreePath().new_first()
        _1, _2, width, height = self.files_view_progress_column.cell_get_size()
        height += self.file_view_column_renderer.get_padding().ypad * 2
        height += 2  # measured - row lines?
        self.scrolled_window.set_min_content_height(height * min(len(self.files_store), 6))

    @throttle(seconds=1)
    def volume_moved(self, button, volume):
        if self.last_known_volume_level != volume:
            self.last_known_volume_level = volume
            self.cast.set_volume(volume)
            print('setting volume', volume)

    @throttle()
    def scrubber_moved(self, scale, scroll_type, seconds):
        print('scrubber_moved', seconds)
        self.seeking = True
        self.cast.media_controller.seek(seconds)

    def humanize_seconds(self, s):
        s = int(s)
        hours = s // (60 * 60)
        minutes = (s // 60) % 60
        seconds = s % 60
        if hours:
            return '%ih %im %is' % (hours, minutes, seconds)
        if minutes:
            return '%im %is' % (minutes, seconds)
        else:
            return '%is' % (seconds)

    def stop_clicked(self, widget):
        if not self.cast: return
        self.cast.media_controller.stop()

    def get_logo_pixbuf(self, width=200, color=None):
        with open(os.path.join(sys.prefix, 'share', 'icons', 'hicolor', 'scalable', 'apps', 'gnomecast.svg')) as f:
            svg = f.read()
        if color:
            svg = svg.replace('#aaaaaa', color)
        f = Gio.MemoryInputStream.new_from_bytes(GLib.Bytes.new(svg.encode()))
        preserve_aspect_ratio = True
        pixbuf = GdkPixbuf.Pixbuf.new_from_stream(f, None)
        return pixbuf

    def quit(self, a=0, b=0):
        for row in self.files_store:
            transcoder = row[7]
            if transcoder:
                transcoder.destroy()
            thumbnail_fn = row[4]
            if thumbnail_fn and os.path.isfile(thumbnail_fn):
                os.remove(thumbnail_fn)
        self.restore_screensaver()
        Gtk.main_quit()

    def forward_clicked(self, widget):
        self.seek_delta(30)

    def rewind_clicked(self, widget):
        self.seek_delta(-10)

    def seek_delta(self, delta):
        seconds = self.cast.media_controller.status.current_time + time.time() - self.last_time_current_time + delta
        self.last_time_current_time = time.time()
        self.cast.media_controller.status.current_time = seconds
        self.scrubber_adj.set_value(seconds)
        self.seeking = True
        self.cast.media_controller.seek(seconds)

    def play_clicked(self, widget):
        if not self.cast:
            print('no cast selected')
            return
        cast = self.cast
        mc = cast.media_controller

        print('mc.status.player_state', mc.status.player_state, self.fn, hash(self.fn))
        if mc.status.player_state in ('IDLE', 'UNKNOWN') or self.last_fn_played != self.fn:
            self.last_fn_played = self.fn
            cast.wait()
            mc = cast.media_controller
            kwargs = {}
            if self.subtitles:
                kwargs['subtitles'] = 'http://%s:%s/subtitles.vtt' % (self.ip, self.port)
            current_time = self.scrubber_adj.get_value()
            if current_time:
                kwargs['current_time'] = current_time
            ext = self.fn.split('.')[-1]
            ext = ''.join(ch for ch in ext if ch.isalnum()).lower()
            # Video content type seems to be unimportant? My chromecast plays the videos anyway.
            mc.play_media('http://%s:%s/media/%s.%s' % (self.ip, self.port, hash(self.fn), ext),
                          'audio/%s' % ext if ext in AUDIO_EXTS else 'video/mp4', **kwargs)
            print(cast.status)
            print(mc.status)
            self.prep_next_transcode()
        elif mc.status.player_state == 'PLAYING':
            mc.pause()
        elif mc.status.player_state == 'PAUSED':
            mc.play()

    def on_file_clicked(self, widget):
        dialog = Gtk.FileChooserDialog("Please choose an audio or video file...", self.win,
                                       Gtk.FileChooserAction.OPEN,
                                       (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK))
        dialog.set_select_multiple(True)

        downloads_dir = os.path.expanduser('~/Downloads')
        if os.path.isdir(downloads_dir):
            dialog.set_current_folder(downloads_dir)

        filter_py = Gtk.FileFilter()
        filter_py.set_name("Videos")
        filter_py.add_mime_type("video/*")
        filter_py.add_mime_type("audio/*")
        dialog.add_filter(filter_py)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            print("Open clicked")
            print("File selected:", dialog.get_filenames())
            self.queue_files(dialog.get_filenames())
            # self.select_file(dialog.get_filename())
        elif response == Gtk.ResponseType.CANCEL:
            print("Cancel clicked")

        dialog.destroy()

    def on_new_subtitle_clicked(self):
        dialog = Gtk.FileChooserDialog("Please choose a subtitle file...", self.win,
                                       Gtk.FileChooserAction.OPEN,
                                       (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK))

        if self.fn:
            dialog.set_current_folder(os.path.dirname(self.fn))

        filter_py = Gtk.FileFilter()
        filter_py.set_name("Subtitles")
        filter_py.add_pattern("*.srt")
        filter_py.add_pattern("*.vtt")
        dialog.add_filter(filter_py)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            print("Open clicked")
            print("File selected: " + dialog.get_filename())
            self.select_subtitles_file(dialog.get_filename())
        elif response == Gtk.ResponseType.CANCEL:
            print("Cancel clicked")
            self.subtitle_combo.set_active(0)

        dialog.destroy()

    def on_download_subtitle_clicked(self):
        dialog = Gtk.FileChooserDialog("Please choose a subtitle file...", self.win,
                                       Gtk.FileChooserAction.OPEN,
                                       (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK))

        if self.fn:
            dialog.set_current_folder(os.path.dirname(self.fn))

        filter_py = Gtk.FileFilter()
        filter_py.set_name("Subtitles")
        filter_py.add_pattern("*.srt")
        filter_py.add_pattern("*.vtt")
        dialog.add_filter(filter_py)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            print("Open clicked")
            print("File selected: " + dialog.get_filename())
            self.select_subtitles_file(dialog.get_filename())
        elif response == Gtk.ResponseType.CANCEL:
            print("Cancel clicked")
            self.subtitle_combo.set_active(0)

        dialog.destroy()

    def select_subtitles_file(self, fn):
        if not os.path.isfile(fn):
            def f():
                dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE, "File Not Found")
                dialog.format_secondary_text("Could not find subtitles file: %s" % fn)
                dialog.run()
                dialog.destroy()

            GLib.idle_add(f)
            return
        fn = os.path.abspath(fn)
        ext = fn.split('.')[-1]
        display_name = os.path.basename(fn)
        if ext == 'vtt':
            with open(fn) as f:
                self.subtitles = f.read()
        else:
            with open(fn, 'rb') as f:
                caps = f.read()
                try:
                    caps = caps.decode()
                except UnicodeDecodeError:
                    caps = caps.decode('latin-1')
            if caps.startswith('\ufeff'):  # BOM
                caps = caps[1:]
            converter = pycaption.CaptionConverter()
            converter.read(caps, pycaption.detect_format(caps)())
            self.subtitles = converter.write(pycaption.WebVTTWriter())
        pos = len(self.subtitle_store)
        stream = StreamMetadata(None, None, title=display_name)
        stream._subtitles = self.subtitles
        self.subtitle_store.append([display_name, stream, None])
        self.subtitle_combo.set_active(pos)

    def unselect_file(self):
        self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
        self.fn = None
        self.stream_store.clear()
        self.subtitle_store.clear()
        self.subtitle_combo.set_active(0)
        self.transcoder = None
        self.duration = None
        if self.cast:
            self.cast.media_controller.stop()

        def f():
            self.scrubber_adj.set_value(0)
            for row in self.files_store:
                row[6] = None
            self.win.resize(1, 1)
            self.update_button_visible()

        GLib.idle_add(f)

    def select_file(self, fn):
        self.unselect_file()
        if not os.path.isfile(fn):
            def f():
                dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE, "File Not Found")
                dialog.format_secondary_text("Could not find media file: %s" % fn)
                dialog.run()
                dialog.destroy()

            GLib.idle_add(f)
            return
        fn = os.path.abspath(fn)
        self.thumbnail_image.set_from_pixbuf(self.get_logo_pixbuf())
        self.fn = fn
        self.stream_store.clear()
        self.subtitle_store.clear()
        if self.cast:
            self.cast.media_controller.stop()

        def f():
            self.scrubber_adj.set_value(0)
            for row in self.files_store:
                thumbnail_fn = row[4]
                if self.fn == row[1]:
                    if thumbnail_fn:
                        self.thumbnail_image.set_from_file(thumbnail_fn)
                        self.win.resize(1, 1)
                    row[6] = 'video-x-generic'
                    self.duration = row[2]
                else:
                    row[6] = None
            threading.Thread(target=self.update_transcoders).start()
            threading.Thread(target=self.update_audio_tracks).start()
            threading.Thread(target=self.update_subtitles).start()
            self.update_button_visible()
            self.update_media_button_states()

        GLib.idle_add(f)

    update_transcoders_lock = threading.Lock()

    def update_transcoders(self):
        with self.update_transcoders_lock:
            if self.cast and self.fn:
                transcoder = None
                for row in self.files_store:
                    if row[1] != self.fn: continue
                    transcoder = row[7]
                    fmd = row[8]
                    fmd.wait()
                    if not self.video_stream:
                        self.video_stream = fmd.video_streams[0]
                    if not self.audio_stream and fmd.audio_streams:
                        self.audio_stream = fmd.audio_streams[0]
                    if (
                        not transcoder
                        or self.cast != transcoder.cast
                        or self.fn != transcoder.source_fn
                        or self.audio_stream != transcoder.audio_stream
                    ):
                        self.transcoder = Transcoder(
                            cast=self.cast,
                            fmd=fmd,
                            video_stream=self.video_stream,
                            audio_stream=self.audio_stream,
                            done_callback=lambda did_transcode=None: GLib.idle_add(self.update_status, did_transcode),
                            error_callback=self.error_callback,
                            prev_transcoder=transcoder,
                        )
                        row[7] = self.transcoder
                if self.autoplay:
                    self.autoplay = False
                    self.play_clicked(None)
            if not self.cast:
                for row in self.files_store:
                    transcoder = row[7]
                    if transcoder:
                        transcoder.destroy()
                        row[7] = None
            GLib.idle_add(self.update_media_button_states)

    def check_for_next_in_queue(self):
        next = False
        for row in self.files_store:
            fn = row[1]
            if next:
                print('check_for_next_in_queue', fn)
                self.autoplay = True
                self.select_file(fn)
                next = False
            if self.cast and self.fn and self.fn == fn:
                next = True

    def prep_next_transcode(self):
        transcode_next = False
        for row in self.files_store:
            fn = row[1]
            transcoder = row[7]
            fmd = row[8]
            if transcode_next and not transcoder:
                print('prep_next_transcode', fn)
                transcoder = Transcoder(
                    cast=self.cast,
                    fmd=fmd,
                    video_stream=fmd.video_streams[0] if fmd.video_streams else None,
                    audio_stream=fmd.audio_streams[0] if fmd.audio_streams else None,
                    done_callback=lambda did_transcode=None: GLib.idle_add(self.update_status, did_transcode),
                    error_callback=self.error_callback,
                    prev_transcoder=transcoder,
                )
                row[7] = transcoder
                transcode_next = False
            if self.cast and self.fn and self.fn == fn and transcoder and transcoder.done:
                transcode_next = True

    def get_info(self, fn):
        cmd = ['ffprobe', '-i', fn]
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT)
        for line in output.decode().split('\n'):
            line = line.strip()
            if line.startswith('Duration:'):
                duration = parse_ffmpeg_time(line.split()[1].strip(','))
                if fn == self.fn:
                    self.duration = duration
                for row in self.files_store:
                    if row[1] == fn:
                        row[2] = duration
                        row[3] = self.humanize_seconds(duration)

    def get_fmd(self):
        for row in self.files_store:
            fn = row[1]
            fmd = row[8]
            if self.fn == fn:
                return fmd

    def update_subtitles(self):
        fmd = self.get_fmd()
        fmd.wait()

        def f():
            self.subtitle_store.clear()
            pos = len(self.subtitle_store)
            for stream in fmd.subtitles:
                self.subtitle_store.append([stream.title, stream, None])
                pos += 1
            self.add_extra_subtitle_options()

        GLib.idle_add(f)
        ext = self.fn.split('.')[-1]
        sexts = ['vtt', 'srt']
        for sext in sexts:
            if os.path.isfile(self.fn[:-len(ext)] + sext):
                self.select_subtitles_file(self.fn[:-len(ext)] + sext)
                break

    def update_audio_tracks(self):
        fmd = self.get_fmd()
        fmd.wait()

        def f():
            self.stream_store.clear()
            for video_stream in fmd.video_streams:
                for audio_stream in fmd.audio_streams:
                    self.stream_store.append(
                        ['%s - %s' % (video_stream.title, audio_stream.title), video_stream, audio_stream])
            self.audio_combo.set_active(0)

        GLib.idle_add(f)

    def on_key_press(self, widget, event, user_data=None):
        key = Gdk.keyval_name(event.keyval)
        ctrl = (event.state & Gdk.ModifierType.CONTROL_MASK)
        if key == 'q' and ctrl:
            self.quit()
            return True
        return False

    def select_cast(self, cast):
        self.cast = cast
        if cast:
            #      cast.media_controller.app_id = 'FF0F6B72'
            self.last_known_volume_level = cast.media_controller.status.volume_level
            self.volume_button.set_value(cast.media_controller.status.volume_level)
        self.last_known_player_state = None
        self.update_media_button_states()
        threading.Thread(target=self.update_transcoders).start()

    def error_callback(self, msg):
        def f():
            dialogWindow = Gtk.MessageDialog(
                self.win,
                Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
                Gtk.MessageType.INFO,
                Gtk.ButtonsType.OK,
                '\nGnomecast encountered an error converting your file.'
            )
            dialogWindow.set_title('Transcoding Error')
            dialogWindow.set_default_size(1, 400)

            dialogBox = dialogWindow.get_content_area()
            buffer1 = Gtk.TextBuffer()
            buffer1.set_text(msg)
            text_view = Gtk.TextView(buffer=buffer1)
            text_view.set_editable(False)
            scrolled_window = Gtk.ScrolledWindow()
            scrolled_window.set_border_width(5)
            # we scroll only if needed
            scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
            scrolled_window.add(text_view)
            dialogBox.pack_end(scrolled_window, True, True, 0)
            dialogWindow.show_all()
            response = dialogWindow.run()
            dialogWindow.destroy()

        GLib.idle_add(f)

    def show_file_info(self, b=None):
        print('show_file_info')
        fmd = self.get_fmd()
        msg = '\n' + fmd.details()
        if self.cast:
            msg += '\nDevice: %s (%s)' % (self.cast.model_name, self.cast.cast_info.manufacturer)
        msg += '\nChromecast: v%s' % __version__
        dialogWindow = Gtk.MessageDialog(
            self.win,
            Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            Gtk.MessageType.INFO,
            Gtk.ButtonsType.OK,
            msg,
        )
        dialogWindow.set_title('File Info')
        dialogWindow.set_default_size(1, 400)

        if self.cast:
            title = 'Error playing %s' % os.path.basename(self.fn)
            body = '''
[Please describe what happened here...]

[Please link to the download here...]

```
[If possible, please run `ffprobe -i <fn>` and paste the output here...]
```

------------------------------------------------------------

%s

%s

```%s```''' % (msg, fmd, fmd._important_ffmpeg)
            url = 'https://github.com/keredson/gnomecast/issues/new?title=%s&body=%s' % (
                urllib.parse.quote(title), urllib.parse.quote(body))
            dialogWindow.add_action_widget(Gtk.LinkButton(url, label="Report File Doesn't Play"), 10)

        dialogBox = dialogWindow.get_content_area()
        buffer1 = Gtk.TextBuffer()
        buffer1.set_text(fmd._ffmpeg_output)
        text_view = Gtk.TextView(buffer=buffer1)
        text_view.set_editable(False)
        scrolled_window = Gtk.ScrolledWindow()
        scrolled_window.set_border_width(5)
        # we scroll only if needed
        scrolled_window.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled_window.add(text_view)
        dialogBox.pack_end(scrolled_window, True, True, 0)

        dialogWindow.show_all()
        response = dialogWindow.run()
        dialogWindow.destroy()

    def get_nonlocal_cast(self):
        dialogWindow = Gtk.MessageDialog(
            self.win,
            Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            Gtk.MessageType.QUESTION,
            Gtk.ButtonsType.OK_CANCEL,
            '\nPlease specify the IP address or hostname of a Chromecast device:',
        )

        dialogWindow.set_title('Add a non-local Chromecast')

        dialogBox = dialogWindow.get_content_area()
        userEntry = Gtk.Entry()
        #    userEntry.set_size_request(250,0)
        dialogBox.pack_end(userEntry, False, False, 0)

        dialogWindow.show_all()
        response = dialogWindow.run()
        text = userEntry.get_text()
        dialogWindow.destroy()
        if (response == Gtk.ResponseType.OK) and (text != ''):
            print(text)
            try:
                cast = pychromecast.Chromecast(text)
                self.cast_store.append([cast, text])
                self.cast_combo.set_active(len(self.cast_store) - 1)
            except pychromecast.error.ChromecastConnectionError:
                dialog = Gtk.MessageDialog(self.win, 0, Gtk.MessageType.ERROR, Gtk.ButtonsType.CLOSE,
                                           "Chromecast Not Found")
                dialog.format_secondary_text("The Chromecast '%s' wasn't found." % text)
                dialog.run()
                dialog.destroy()

    def on_cast_combo_changed(self, combo):
        tree_iter = combo.get_active_iter()
        if tree_iter is not None:
            model = combo.get_model()
            cast, name = model[tree_iter][:2]
            if cast == -1:
                self.get_nonlocal_cast()
            else:
                print(cast)
                self.select_cast(cast)
        else:
            entry = combo.get_child()

    def on_subtitle_combo_changed(self, combo):
        tree_iter = combo.get_active_iter()
        if tree_iter is not None:
            model = combo.get_model()
            text, stream, callback = model[tree_iter]
            print('chose subtitle', text, stream, callback)
            if callback:
                callback()
            else:
                self.subtitles = stream._subtitles if stream else None
                mc = self.cast.media_controller if self.cast else None
                if mc and mc.status.player_state in ('BUFFERING', 'PLAYING', 'PAUSED'):
                    self.stop_clicked(None)
                    self.cast.wait()

                    def f(): self.play_clicked(None)

                    threading.Timer(1, lambda: GLib.idle_add(f)).start()
        else:
            entry = combo.get_child()

    def on_audio_combo_changed(self, combo):
        tree_iter = combo.get_active_iter()
        if tree_iter is not None:
            model = combo.get_model()
            text, video_stream, audio_stream = model[tree_iter]
            print(text, video_stream, audio_stream)
            self.video_stream = video_stream
            self.audio_stream = audio_stream
            threading.Thread(target=self.update_transcoders).start()


def arg_parse(args, kw_synonyms, f, usage):
    kw = None
    f_args = []
    f_kwargs = {}
    for arg in args:
        if arg.startswith('-'):
            if kw:
                f_kwargs[kw] = True
            arg = arg.lstrip('-')
            kw = kw_synonyms.get(arg, arg)
        else:
            if kw:
                f_kwargs[kw] = arg
            else:
                f_args.append(arg)
            kw = None
    if kw:
        f_kwargs[kw] = True
    try:
        f(*f_args, **f_kwargs)
    except TypeError as e:
        msg = str(e).split('()', 1)[1].strip()
        print('ERROR:', msg)
        print(usage)
        sys.exit(1)


USAGE = '''
python gnomecast.py [<media_filename>] [-d|--device <chromecast_name>] [-s|--subtitles <subtitles_filename>]
'''.strip()


def pid_running(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    else:
        return True


def delete_old_transcodes():
    # if process is killed old transcoded files can be left around
    # delete if found
    for tmpdir in ['/tmp', '/var/tmp']:
        for fn in os.listdir(tmpdir):
            if not fn.startswith('gnomecast_'): continue
            fn = os.path.join(tmpdir, fn)
            match = re.search(r'gnomecast_pid(\d+)_', fn)
            if match:
                pid = int(match.group(1))
                if not pid_running(pid):
                    print('\tpid', pid, 'is dead, so deleting', fn)
                    os.remove(fn)
            else:
                print('old style gnomecast file', fn, 'found, so deleting...')
                os.remove(fn)


def main():
    delete_old_transcodes()
    caster = Gnomecast()
    arg_parse(sys.argv[1:], {'s': 'subtitles', 'd': 'device'}, caster.run, USAGE)


if DEPS_MET and __name__ == '__main__':
    main()