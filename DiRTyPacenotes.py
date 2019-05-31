#!python3
#
# DiRTy Pacenotes
#
# Copyright [2017 - 2019] [Palo Samo]
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import csv
import glob
import os
import socket
import struct
import sys
import itertools
import ast
import math
import win32gui, win32con
import wx
import wx.adv
import wx.aui
import wx.grid
import wx.lib.intctrl as ict
import wx.lib.scrolledpanel as scr
import wx.lib.agw.ultimatelistctrl as ulc
import wx.lib.agw.flatnotebook as fnb
import wx.lib.agw.persist as per
from wx.lib.wordwrap import wordwrap
from pubsub import pub
from collections import defaultdict, OrderedDict
from configobj import ConfigObj
from threading import Thread
from queue import Queue
from pydub import AudioSegment
from pydub.playback import play
from pathlib import Path


hide = win32gui.GetForegroundWindow()
win32gui.ShowWindow(hide, win32con.SW_HIDE)

app_path = os.getcwd()
data_path = os.path.join(app_path, 'data')
img_path = os.path.join(data_path, 'images')
config_ini = os.path.join(data_path, 'config.ini')
sound_bank = {}
q_snd = Queue()
q_run = Queue()
q_rst = Queue()
q_del = Queue()
q_vol = Queue()
q_dic = Queue()
q_cfg = Queue()
q_stg = Queue()


# UDP server
class Reader(Thread):
    def __init__(self):
        Thread.__init__(self)

        if not q_cfg.empty():
            config = q_cfg.get_nowait()
            q_cfg.task_done()
            self.server = config[0]
            self.co_driver = config[1]
            self.delay = config[2]
            self.volume = config[3]
            self.countdown = config[4]
        co_path = os.path.join(app_path, 'co-drivers', self.co_driver)
        self.pace_path = os.path.join(co_path, 'pacenotes')
        self.snd_path = os.path.join(co_path, 'sounds')
        if not q_stg.empty():
            self.dic_stages = q_stg.get_nowait()
            q_stg.task_done()
        self.dic_pacenotes = OrderedDict()
        self.dic_new_pacenotes = OrderedDict()
        self.new_dist = 0
        self.pos_y = 0
        self.total_laps = 0
        self.lap_time = 0
        self.stage_length = 0
        self.snd_ext = ''
        self.stage_path = ''
        self.stage_name = ''
        self.stage_name_dic = ''
        self.stage_folder = ''
        self.stage_file = ''
        self.count_played = False
        self.restart = False

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(self.server)

        self.running = True
        self.setDaemon(True)
        self.start()

    def run(self):
        try:
            snd_file_list = q_snd.get_nowait()
            loaded = 0
            for snd_file in snd_file_list:
                sound = Path(snd_file).stem
                snd, self.snd_ext = os.path.splitext(snd_file)
                try:
                    sound_bank[sound] = AudioSegment.from_file(snd_file)
                except IndexError:
                    continue
                loaded += 1
                wx.CallAfter(pub.sendMessage, 'get_progress', arg=loaded)
        except IOError:
            pass

        while self.running:
            self.receive_udp_packet()  # Has its own breakable while loop.
            self.detect_stage()
            self.read_pacenotes_file()
            self.receive_udp_stream()  # Has its own infinite while loop.
        self.sock.shutdown(socket.SHUT_RD)
        self.sock.close()

    # Perform initial UDP detection.
    def receive_udp_packet(self):
        while True:
            udp_stream = self.sock.recv(512)
            if not udp_stream:
                break  # lost connection
            udp_data = struct.unpack('64f', udp_stream[0:256])
            total_time = int(udp_data[0])
            self.pos_y = int(udp_data[5])
            curr_lap = int(udp_data[59])
            self.total_laps = int(udp_data[60])
            self.stage_length = round(udp_data[61], 4)

            # wx.CallAfter(pub.sendMessage, 'get_stage_length', arg=self.stage_length)
            if total_time == 0 != curr_lap:  # Wait for udp from next stage after finish.
                continue
            break

    # Detect stage.
    def detect_stage(self):
        for k, v in list(self.dic_stages.items()):
            if self.stage_length == k and self.total_laps == 1:  # Rally stage indicator.
                for s in v:
                    if len(v) == 1:
                        stg = s.split(',')
                        self.stage_name_dic = stg[1]
                        self.stage_folder = stg[2]
                    elif len(v) == 2:
                        stg = s.split(',')
                        pos_start = int(stg[0])
                        if self.pos_y == pos_start:
                            self.stage_name_dic = stg[1]
                            self.stage_folder = stg[2]
        if self.stage_name_dic != self.stage_name:
            self.stage_name = self.stage_name_dic
            self.stage_path = os.path.join(self.pace_path, self.stage_folder)
            self.stage_file = os.path.join(self.stage_path, self.stage_name + '.txt')
            wx.CallAfter(pub.sendMessage, 'get_stage', arg1=self.stage_name, arg2=self.stage_path)

    # Read pacenotes file.
    def read_pacenotes_file(self):
        self.dic_pacenotes.clear()
        with open(self.stage_file, 'r') as f:
            for line in f:
                if line and line.strip():
                    lis = line.split(',')  # list [curr_dist, sound]
                    key = int(lis[0])  # key as integer
                    val = lis[1].strip()  # value as string
                    self.dic_pacenotes[key] = []  # empty list
                    self.dic_pacenotes[key].append(val)  # dictionary
                else:
                    continue  # skip empty lines

    # Receive UDP stream.
    def receive_udp_stream(self):
        last_dist = -20
        last_time = 0

        # Play countdown sound.
        try:
            if self.countdown and not self.count_played and last_time == 0:
                sound_count = sound_bank['countdown_start'] + self.volume
                play(sound_count)
                self.count_played = True
        except KeyError:
            wx.CallAfter(pub.sendMessage, 'key_error', arg='countdown_start')
            return

        while self.running:
            if not q_run.empty():
                self.running = q_run.get_nowait()
                q_run.task_done()
            if not q_rst.empty():
                reset = q_rst.get_nowait()
                q_rst.task_done()
                if reset is True:
                    return
            if not q_del.empty():
                self.delay = q_del.get_nowait()
                q_del.task_done()
            if not q_vol.empty():
                self.volume = q_vol.get_nowait()
                q_vol.task_done()
            if not q_dic.empty():
                self.dic_pacenotes.clear()
                dic_pace = q_dic.get_nowait()
                q_dic.task_done()
                for key, val in list(dic_pace.items()):
                    self.dic_pacenotes[int(key)] = []
                    self.dic_pacenotes[int(key)].append(val.strip())
            udp_stream = self.sock.recv(512)
            if not udp_stream:
                break  # lost connection
            udp_data = struct.unpack('64f', udp_stream[0:256])
            total_time = udp_data[0]
            lap_time = int(udp_data[1])
            curr_dist = int(udp_data[2])
            curr_lap = int(udp_data[59])

            if total_time == last_time and lap_time == 0:
                self.restart = True
            else:
                self.restart = False
            wx.CallAfter(pub.sendMessage, 'get_pause', arg=self.restart)

            # Play sounds.
            if lap_time > 0:  # Timing clock started.
                self.count_played = False
                if curr_lap == 0:  # Car on stage but before finish line.
                    wx.CallAfter(pub.sendMessage, 'get_dist', arg1=curr_dist, arg2=last_dist)
                    self.dic_new_pacenotes.clear()
                    for dist, pace in list(self.dic_pacenotes.items()):
                        if curr_dist < self.delay:
                            self.new_dist = math.ceil(dist / 2)
                        elif curr_dist >= self.delay:
                            self.new_dist = dist - self.delay
                        self.dic_new_pacenotes[self.new_dist] = pace
                    for new_dist, new_pace in list(self.dic_new_pacenotes.items()):
                        if curr_dist == new_dist:
                            if curr_dist > last_dist:  # Play pacenotes.
                                for curr_pace in new_pace:
                                    snd = curr_pace.split()
                                    for sound_name in snd:
                                        try:
                                            sound_pace = sound_bank[sound_name] + self.volume
                                            play(sound_pace)
                                        except KeyError:
                                            wx.CallAfter(pub.sendMessage, 'key_error', arg=sound_name)
                                            pass
                            elif 0 < curr_dist < last_dist:  # Play wrong_way.
                                try:
                                    sound_wrong = sound_bank['wrong_way'] + self.volume
                                    play(sound_wrong)
                                except KeyError:
                                    wx.CallAfter(pub.sendMessage, 'key_error', arg='wrong_way')
                                    pass
                elif curr_lap == 1:  # Stage is finished.
                    break
                last_dist = curr_dist
            elif lap_time == 0:  # Timing clock not started.
                break
            last_time = total_time


class MenuBar(wx.MenuBar):
    def __init__(self, parent):
        super(MenuBar, self).__init__()

        self.parent = parent

        # File Menu.
        self.file_menu = wx.Menu()

        self.menu_open = wx.MenuItem(self.file_menu, wx.ID_OPEN, wx.GetStockLabel(wx.ID_OPEN) + '\tCtrl+O',
                                     'Open existing pacenotes file')
        self.menu_open.SetBitmap(wx.Bitmap(os.path.join(img_path, 'open.png')))
        self.menu_save = wx.MenuItem(self.file_menu, wx.ID_SAVE, wx.GetStockLabel(wx.ID_SAVE) + '\tCtrl+S',
                                     'Overwrite current pacenotes file')
        self.menu_save.SetBitmap(wx.Bitmap(os.path.join(img_path, 'save.png')))
        self.menu_creator = wx.MenuItem(self.file_menu, wx.ID_ANY, 'Creator' + '\tCtrl+R',
                                        'Change co-driver pacenote commands')
        self.menu_creator.SetBitmap(wx.Bitmap(os.path.join(img_path, 'settings.png')))
        self.menu_settings = wx.MenuItem(self.file_menu, wx.ID_ANY, 'Settings' + '\tCtrl+T',
                                         'Change app settings')
        self.menu_settings.SetBitmap(wx.Bitmap(os.path.join(img_path, 'settings.png')))
        self.menu_quit = wx.MenuItem(self.file_menu, wx.ID_EXIT, wx.GetStockLabel(wx.ID_EXIT) + '\tCtrl+Q',
                                     'Close the app')
        self.menu_quit.SetBitmap(wx.Bitmap(os.path.join(img_path, 'exit.png')))

        self.file_menu.Append(self.menu_open)
        self.file_menu.Append(self.menu_save)
        self.file_menu.AppendSeparator()
        self.file_menu.Append(self.menu_creator)
        self.file_menu.Append(self.menu_settings)
        self.file_menu.AppendSeparator()
        self.file_menu.Append(self.menu_quit)
        self.menu_save.Enable(False)

        self.Bind(wx.EVT_MENU, self.parent.on_open, self.menu_open)
        self.Bind(wx.EVT_MENU, self.parent.on_save, self.menu_save)
        self.Bind(wx.EVT_MENU, self.parent.on_creator, self.menu_creator)
        self.Bind(wx.EVT_MENU, self.parent.on_settings, self.menu_settings)
        self.Bind(wx.EVT_MENU, self.parent.on_quit, self.menu_quit)

        # Edit Menu.
        self.edit_menu = wx.Menu()
        # self.menu_cut = wx.MenuItem(self.file_menu, wx.ID_CUT, wx.GetStockLabel(wx.ID_CUT) + '\tCtrl+X',
        #                        'Cut selected text')
        # self.menu_cut.SetBitmap(wx.Bitmap('data/images/cut.png', wx.BITMAP_TYPE_PNG))
        # self.menu_copy = wx.MenuItem(self.file_menu, wx.ID_COPY, wx.GetStockLabel(wx.ID_COPY) + '\tCtrl+C',
        #                         'Copy selected text')
        # self.menu_copy.SetBitmap(wx.Bitmap('data/images/copy.png', wx.BITMAP_TYPE_PNG))
        # self.menu_paste = wx.MenuItem(self.file_menu, wx.ID_PASTE, wx.GetStockLabel(wx.ID_PASTE) + '\tCtrl+V',
        #                          'Paste text from clipboard')
        # self.menu_paste.SetBitmap(wx.Bitmap('data/images/paste.png', wx.BITMAP_TYPE_PNG))
        # self.menu_delete = wx.MenuItem(self.file_menu, wx.ID_DELETE, wx.GetStockLabel(wx.ID_DELETE) + '\tDel',
        #                           'Delete selected text')
        # self.menu_delete.SetBitmap(wx.Bitmap('data/images/delete.png', wx.BITMAP_TYPE_PNG))
        self.menu_select_all = wx.MenuItem(self.file_menu, 20000, 'Select All',
                                           'Select all lines of pacenotes', wx.ITEM_CHECK)
        # self.edit_menu.Append(self.menu_cut)
        # self.edit_menu.Append(self.menu_copy)
        # self.edit_menu.Append(self.menu_paste)
        # self.edit_menu.AppendSeparator()
        # self.edit_menu.Append(self.menu_delete)
        self.edit_menu.Append(self.menu_select_all)

        # self.Bind(wx.EVT_TEXT_CUT, self.menu_cut)
        # self.Bind(wx.EVT_TEXT_COPY, self.menu_copy)
        # self.Bind(wx.EVT_TEXT_PASTE, self.menu_paste)
        # self.Bind(wx.EVT_TEXT, self.menu_delete)
        self.Bind(wx.EVT_MENU, self.parent.on_tick, self.menu_select_all)

        # Autosave Menu.
        self.autosave_menu = wx.Menu()
        self.radio_off = self.autosave_menu.AppendRadioItem(1000, 'OFF')
        self.radio_two = self.autosave_menu.AppendRadioItem(2, '2 min')
        self.radio_five = self.autosave_menu.AppendRadioItem(5, '5 min')
        self.radio_ten = self.autosave_menu.AppendRadioItem(10, '10 min')
        for radio in [self.radio_off, self.radio_two, self.radio_five, self.radio_ten]:
            if int(self.parent.interval) == radio.GetId():
                radio.Check()

            self.Bind(wx.EVT_MENU, self.parent.on_interval, radio)

        # Delay Menu.
        self.delay_menu = wx.Menu()
        self.delay_recce = self.delay_menu.AppendRadioItem(100, 'Recce')
        self.delay_late = self.delay_menu.AppendRadioItem(150, 'Late')
        self.delay_normal = self.delay_menu.AppendRadioItem(200, 'Normal')
        self.delay_earlier = self.delay_menu.AppendRadioItem(250, 'Earlier')
        self.delay_early = self.delay_menu.AppendRadioItem(300, 'Very Early')
        for radio in [self.delay_recce, self.delay_late, self.delay_normal, self.delay_earlier, self.delay_early]:
            if self.parent.delay == radio.GetId():
                radio.Check()

            self.Bind(wx.EVT_MENU, self.parent.on_delay, radio)
        # self.delay_menu.InsertSeparator(4)

        # Help Menu.
        self.help_menu = wx.Menu()
        self.menu_about = wx.MenuItem(self.help_menu, wx.ID_ABOUT, wx.GetStockLabel(wx.ID_ABOUT), 'About this app')
        self.menu_about.SetBitmap(wx.Bitmap(os.path.join(img_path, 'about.png')))
        self.help_menu.Append(self.menu_about)

        self.Bind(wx.EVT_MENU, self.parent.on_about, self.menu_about)


class TaskBar(wx.adv.TaskBarIcon):
    def __init__(self, frame):
        wx.adv.TaskBarIcon.__init__(self)

        self.frame = frame
        self.SetIcon(frame.icon, frame.title)
        self.Bind(wx.EVT_MENU, self.on_show, id=1)
        self.Bind(wx.EVT_MENU, self.on_hide, id=2)
        self.Bind(wx.EVT_MENU, self.on_close, id=3)

    def CreatePopupMenu(self):
        menu = wx.Menu()
        menu.Append(1, 'Show')
        menu.Append(2, 'Hide')
        menu.Append(3, 'Close')
        return menu

    def on_show(self, event):
        if not self.frame.IsShown():
            self.frame.Show()

    def on_hide(self, event):
        if self.frame.IsShown():
            self.frame.Hide()

    def on_close(self, event):
        self.frame.Close()


class HandInput(wx.Dialog):  # not used at the moment
    def __init__(self, parent):
        wx.Dialog.__init__(self, parent)

        self.parent = parent
        self.SetSize(wx.Size(180, 80))
        self.SetTitle('DiRTy Handbrake')
        self.SetIcon(self.parent.icon)
        self.Center(wx.BOTH)

        panel = wx.Panel(self, name='panel_handbrake')
        box_main = wx.BoxSizer(wx.HORIZONTAL)

        label_handbrake = wx.StaticText(panel, 0, 'APPLY HANDBRAKE')
        box_main.Add(label_handbrake, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.ALL, 15)
        panel.SetSizer(box_main)

        # self.SetReturnCode()


class TextDropTarget(wx.TextDropTarget):
    def __init__(self, target):
        wx.TextDropTarget.__init__(self)
        self.target = target

    def OnDropText(self, x, y, data):
        self.target.InsertItem(sys.maxsize, data)
        return True


class Settings(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(self, parent)

        self.parent = parent
        self.SetSize(wx.Size(260, 340))
        self.SetTitle('DiRTy Pacenotes - Service Area')
        self.SetIcon(self.parent.icon)
        self.Center(wx.BOTH)

        panel = wx.Panel(self, name='panel_settings')
        box_main = wx.BoxSizer(wx.VERTICAL)

        box_server = wx.StaticBox(panel, 0, 'UDP SERVER')
        sbs_server = wx.StaticBoxSizer(box_server)

        label_ip = wx.StaticText(panel, 0, 'IP')
        self.ip_value = wx.TextCtrl(panel, size=wx.Size(60, 23))
        self.ip_value.SetValue(self.parent.ip)
        sbs_server.Add(label_ip, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        sbs_server.Add(self.ip_value, 0, wx.ALL, 10)
        label_port = wx.StaticText(panel, 0, 'Port')
        self.port_value = ict.IntCtrl(panel, size=wx.Size(45, 23), min=10000, max=99999, value=self.parent.port,
                                      limited=True, allow_none=True)
        self.port_value.SetValue(self.parent.port)
        sbs_server.Add(label_port, 0, wx.ALIGN_CENTER_VERTICAL)
        sbs_server.Add(self.port_value, 0, wx.ALL, 10)

        box_co_driver = wx.StaticBox(panel, 0, 'CO-DRIVER')
        sbs_co_driver = wx.StaticBoxSizer(box_co_driver)

        co_drivers = os.listdir('co-drivers')
        self.combo_co_driver = wx.ComboBox(panel, choices=co_drivers, style=wx.CB_READONLY)
        self.combo_co_driver.SetValue(self.parent.co_driver)
        self.combo_co_driver.SetFocus()
        sbs_co_driver.Add(self.combo_co_driver, 0, wx.ALL, 10)

        box_countdown = wx.BoxSizer(wx.HORIZONTAL)
        self.count_check = wx.CheckBox(panel, 0, 'COUNTDOWN')
        self.count_check.SetValue(bool(self.parent.countdown))
        box_countdown.Add(self.count_check, 0, wx.ALL, 10)

        '''
        box_handbrake = wx.StaticBox(panel, 0, 'HANDBRAKE')
        sbs_handbrake = wx.StaticBoxSizer(box_handbrake)

        button_handbrake = wx.Button(panel, wx.ID_ANY, 'CHANGE')
        button_handbrake.Bind(wx.EVT_BUTTON, self.parent.on_change_handbrake)
        self.handbrake_value = wx.TextCtrl(panel, size=wx.Size(60, 23))
        self.handbrake_value.SetValue(self.parent.handbrake)
        sbs_handbrake.Add(self.handbrake_value, 0, wx.ALL, 10)
        sbs_handbrake.Add(button_handbrake, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 10)
        '''
        bs_buttons = wx.BoxSizer(wx.HORIZONTAL)
        button_reload = wx.Button(panel, id=2, label='SAVE and RELOAD')
        button_reload.Bind(wx.EVT_BUTTON, self.parent.on_reload)

        bs_buttons.Add(button_reload, 0)

        box_main.Add(sbs_server, 0, wx.ALL, 20)
        box_main.Add(sbs_co_driver, 0, wx.LEFT, 20)
        box_main.Add(box_countdown, 0, wx.LEFT, 20)
        # box_main.Add(sbs_handbrake, 0, wx.LEFT | wx.RIGHT, 20)
        box_main.Add(bs_buttons, 0, wx.ALIGN_CENTER_HORIZONTAL | wx.TOP, 20)
        panel.SetSizer(box_main)


class Creator(wx.Dialog):
    def __init__(self, parent):
        wx.Dialog.__init__(self, parent)

        self.parent = parent
        self.SetSize(wx.Size(640, 480))
        self.SetTitle('DiRTy Pacenotes - ' + self.parent.co_driver)
        self.SetBackgroundColour('light grey')
        self.SetIcon(self.parent.icon)
        self.Center(wx.BOTH)
        self.SetWindowStyle(wx.DEFAULT_DIALOG_STYLE)

        self.dict_list_c = {}
        self.cat_list_c = []
        self.sound_list_c = []
        self.audio_list_c = []
        self.selection_left = []
        self.selection_right = []

        panel_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # Left side
        box_left = wx.BoxSizer(wx.HORIZONTAL)
        self.tabs_left = fnb.FlatNotebook(self, agwStyle=fnb.FNB_HIDE_ON_SINGLE_TAB)
        box_left.Add(self.tabs_left, 1, wx.EXPAND)

        # Right side
        box_right = wx.BoxSizer(wx.VERTICAL)

        # Panel with buttons (right top)
        box_but_top = wx.BoxSizer(wx.HORIZONTAL)
        box_but_bot = wx.BoxSizer(wx.HORIZONTAL)

        self.button_in = wx.Button(self, wx.ID_ANY, label=u'IN')
        self.button_out = wx.Button(self, wx.ID_ANY, label=u'OUT')
        button_add = wx.Button(self, wx.ID_ANY, label='ADD CATEGORY')
        button_reload = wx.Button(self, id=1, label='SAVE and RELOAD')
        button_reset = wx.Button(self, wx.ID_ANY, label='RESET SOUNDS')

        self.button_in.Disable()
        self.button_out.Disable()

        box_but_top.Add(self.button_in, 0, wx.ALL, 5)
        box_but_top.Add(self.button_out, 0, wx.ALL, 5)
        box_but_top.Add(button_add, 0, wx.ALL, 5)
        box_but_bot.Add(button_reset, 0, wx.ALL, 5)
        box_but_bot.Add(button_reload, 0, wx.ALL, 5)

        self.button_in.Bind(wx.EVT_BUTTON, self.parent.sounds_in)
        self.button_out.Bind(wx.EVT_BUTTON, self.parent.sounds_out)
        button_reset.Bind(wx.EVT_BUTTON, self.parent.reset_sounds)
        button_reload.Bind(wx.EVT_BUTTON, self.parent.on_reload)
        button_add.Bind(wx.EVT_BUTTON, self.parent.add_category)

        # Panel with categories (right bottom)
        box_cat = wx.BoxSizer(wx.HORIZONTAL)
        self.tabs_right = wx.aui.AuiNotebook(self, style=wx.aui.AUI_NB_WINDOWLIST_BUTTON | wx.aui.AUI_NB_TAB_MOVE |
                                             wx.aui.AUI_NB_SCROLL_BUTTONS | wx.aui.AUI_NB_CLOSE_BUTTON)
        self.tabs_right.Bind(wx.aui.EVT_AUINOTEBOOK_PAGE_CLOSE, self.parent.on_tab_close)
        box_cat.Add(self.tabs_right, 1, wx.EXPAND)

        box_right.Add(box_but_top, 0, wx.ALIGN_CENTER_HORIZONTAL)
        box_right.Add(box_cat, 2, wx.EXPAND | wx.ALL, 5)
        box_right.Add(box_but_bot, 0, wx.ALIGN_CENTER_HORIZONTAL)

        # Add sizers to panel_sizer.
        panel_sizer.Add(box_left, 1, wx.EXPAND | wx.ALL, 10)
        panel_sizer.Add(box_right, 0, wx.EXPAND | wx.ALL, 10)
        self.SetSizer(panel_sizer)

        self.create_audio()
        self.create_sounds()

    # Define methods.
    def create_audio(self):
        self.tabs_left.DeleteAllPages()
        for s in os.listdir(self.parent.sound_path):
            (name, ext) = s.split('.')
            self.audio_list_c.append(name)
        for sublist in list(self.parent.sound_list.values()):
            for item in sublist:
                self.sound_list_c.append(item)
        audio_list_final = self.parent.diff(self.audio_list_c, self.sound_list_c)
        audio_list_final.sort()
        tab_left = wx.Panel(self.tabs_left, style=wx.BORDER_NONE, id=1)
        tab_left.SetBackgroundColour('white')
        tab_left.SetCursor(wx.Cursor(wx.CURSOR_HAND))
        list_box_left = wx.ListBox(tab_left, choices=audio_list_final, style=wx.LB_MULTIPLE)
        h_box_tabs = wx.BoxSizer(wx.HORIZONTAL)
        h_box_tabs.Add(list_box_left, 0, wx.EXPAND)
        tab_left.SetSizer(h_box_tabs)
        list_box_left.Bind(wx.EVT_LISTBOX, self.parent.on_listbox_left)
        self.tabs_left.AddPage(tab_left, 'audio')

    def create_sounds(self):
        self.tabs_right.DeleteAllPages()
        for category, sounds_list in list(self.parent.sound_list.items()):
            tab_right = wx.Panel(self.tabs_right, style=wx.TAB_TRAVERSAL | wx.BORDER_NONE, name=category, id=2)
            tab_right.SetBackgroundColour('white')
            tab_right.SetCursor(wx.Cursor(wx.CURSOR_HAND))
            list_box_right = wx.ListBox(tab_right, choices=sounds_list, style=wx.LB_MULTIPLE)
            h_box_tabs = wx.BoxSizer(wx.HORIZONTAL)
            h_box_tabs.Add(list_box_right, 0, wx.EXPAND)
            tab_right.SetSizer(h_box_tabs)
            list_box_right.Bind(wx.EVT_LISTBOX, self.parent.on_listbox_right)
            self.tabs_right.AddPage(tab_right, category)


class Editor(wx.Window):
    def __init__(self, parent):
        wx.Window.__init__(self, parent)

        self.parent = parent
        self.SetBackgroundColour('white')
        self.SetWindowStyle(wx.BORDER_THEME)

        # SCROLLED PANEL #
        self.scrolled_panel = scr.ScrolledPanel(self, style=wx.BORDER_NONE)
        self.scrolled_panel.SetupScrolling(scroll_x=False, rate_y=8, scrollToTop=False)
        self.scrolled_panel.SetBackgroundColour('white')
        self.scrolled_panel.SetAutoLayout(1)

        logo = wx.StaticBitmap(self.scrolled_panel)
        logo.SetBitmap(wx.Bitmap(os.path.join(img_path, 'logo.png')))

        self.v_box = wx.BoxSizer(wx.VERTICAL)
        self.scrolled_panel.SetSizer(self.v_box)

        # BUTTONS #
        self.button_add = wx.Button(self, label='ADD')
        self.button_insert = wx.Button(self, label='INSERT')
        self.button_replace = wx.Button(self, label='REPLACE')
        self.button_delete = wx.Button(self, label='DELETE')
        # self.button_undo_pace = but.GenBitmapButton(self, bitmap=wx.Bitmap(
        #     os.path.join(img_path, 'undo.png')), size=(25, 25))

        self.buttons = (self.button_add, self.button_insert, self.button_replace, self.button_delete)
        for button in self.buttons:
            button.Disable()

        self.h_box_buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.h_box_buttons.Add(self.button_add, 0, wx.RIGHT, 24)
        self.h_box_buttons.Add(self.button_insert, 0, wx.RIGHT, 24)
        self.h_box_buttons.Add(self.button_replace, 0, wx.RIGHT, 24)
        self.h_box_buttons.Add(self.button_delete, 0)
        # self.h_box_buttons.Add(self.button_undo_pace, 0, wx.RIGHT, 10)

        self.Bind(wx.EVT_BUTTON, self.parent.on_add, self.button_add)
        self.Bind(wx.EVT_BUTTON, self.parent.on_insert, self.button_insert)
        self.Bind(wx.EVT_BUTTON, self.parent.on_replace, self.button_replace)
        self.Bind(wx.EVT_BUTTON, self.parent.on_delete, self.button_delete)

        # INPUT BOXES #
        self.input_dist = ict.IntCtrl(self, name='input', size=wx.Size(45, 23), min=0, max=19999,
                                      limited=True, allow_none=False)
        self.input_dist.Disable()
        self.input_pace = wx.SearchCtrl(self, style=wx.TE_READONLY, size=wx.Size(0, 23))
        self.input_pace.SetCancelBitmap(wx.Bitmap(os.path.join(img_path, 'clear.png')))
        self.input_pace.SetCursor(wx.Cursor(wx.CURSOR_ARROW))
        self.input_pace.ShowSearchButton(False)
        self.input_pace.ShowCancelButton(True)
        self.input_pace.SetHint('pacenotes')
        self.input_pace.Bind(wx.EVT_SEARCHCTRL_CANCEL_BTN, self.parent.on_cancel)

        self.button_play = wx.Button(self)
        self.button_play.SetInitialSize(wx.Size(24, 24))
        self.button_play.SetBitmap(wx.Bitmap(os.path.join(img_path, 'sound_on.png')))
        # button_sound.SetBitmapPressed(wx.Bitmap(os.path.join(img_path, 'sound_off.png')))
        self.button_play.Disable()

        self.h_box_input = wx.BoxSizer(wx.HORIZONTAL)
        self.h_box_input.Add(self.input_dist, 0)
        self.h_box_input.Add(self.input_pace, 1, wx.RIGHT, 10)
        self.h_box_input.Add(self.button_play, 0)

        self.input_dist.Bind(wx.lib.intctrl.EVT_INT, self.parent.on_distance)
        self.button_play.Bind(wx.EVT_BUTTON, self.parent.on_play)
        # self.input_dist.Bind(wx.EVT_KEY_UP, self.on_distance)  # Keyboard.
        # self.input_dist.Bind(wx.EVT_TEXT, self.on_distance)  # UDP stream.

        # LABELS #
        self.label_co_driver = wx.StaticText(self, label=self.parent.co_driver + '   |')
        self.label_co_driver.SetForegroundColour('dark grey')
        self.label_co_driver.SetFont(self.parent.font.Bold())

        self.label_delay = wx.StaticText(self, label=self.parent.delay_mode)
        self.label_delay.SetFont(self.parent.font.Bold())
        self.label_delay.SetForegroundColour('dark grey')

        # ONLY FOR GETTING TRACK LENGTH #
        # self.label_length = wx.StaticText(self)
        # self.label_length.SetFont(self.parent.font.Bold())
        # self.label_length.SetForegroundColour('white')
        # self.label_length.Bind(wx.lib.intctrl.EVT_INT, self.parent.update_length, self.label_length)

        label_volume = wx.StaticText(self, label='vol')
        label_volume.SetForegroundColour('dark grey')

        self.slider_volume = wx.Slider(self, wx.ID_ANY, int(self.parent.volume), 0, 10, wx.DefaultPosition, (100, 0),
                                       wx.SL_MIN_MAX_LABELS)
        self.slider_volume.SetTickFreq(1)
        self.slider_volume.SetForegroundColour('dark grey')
        self.slider_volume.Disable()
        self.slider_volume.Bind(wx.EVT_SLIDER, self.parent.on_slider)

        self.h_box_labels = wx.BoxSizer(wx.HORIZONTAL)
        self.h_box_labels.Add(self.label_co_driver, 0, wx.LEFT | wx.RIGHT, 10)
        self.h_box_labels.Add(self.label_delay, 0, wx.RIGHT, 10)
        # self.h_box_labels.Add(self.label_length, 0, wx.TEXT_ALIGNMENT_CENTER)
        self.h_box_labels.AddStretchSpacer(1)
        self.h_box_labels.Add(label_volume, 0, wx.ALIGN_RIGHT)
        self.h_box_labels.Add(self.slider_volume, 0, wx.ALIGN_RIGHT | wx.LEFT | wx.RIGHT, 10)

        # TABS NOTEBOOK #
        self.tabs = wx.aui.AuiNotebook(self, style=wx.aui.AUI_NB_WINDOWLIST_BUTTON | wx.aui.AUI_NB_SCROLL_BUTTONS)
        # self.tabs.SetName('Sounds')

        # Add sizers to panel_sizer.
        panel_sizer = wx.BoxSizer(wx.VERTICAL)
        panel_sizer.Add(self.scrolled_panel, 1, wx.EXPAND | wx.ALL, 10)
        panel_sizer.AddSpacer(5)
        panel_sizer.Add(self.h_box_buttons, 0, wx.EXPAND | wx.ALIGN_LEFT | wx.LEFT, 20)
        panel_sizer.AddSpacer(5)
        panel_sizer.Add(self.h_box_input, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 10)
        panel_sizer.AddSpacer(5)
        panel_sizer.Add(self.h_box_labels, 0, wx.EXPAND | wx.ALL, 10)
        panel_sizer.AddSpacer(5)
        panel_sizer.Add(self.tabs, 1, wx.EXPAND | wx.RIGHT | wx.LEFT | wx.BOTTOM, 10)

        self.SetSizer(panel_sizer)


class DiRTyPacenotes(wx.Frame):
    def __init__(self, *args, **kwargs):
        super(DiRTyPacenotes, self).__init__(*args, **kwargs)

        self.SetName('frame_main')
        self.title = 'DiRTy Pacenotes'
        self.icon = wx.Icon(os.path.join(img_path, 'favicon.ico'))
        self.font = wx.SystemSettings.GetFont(wx.SYS_DEFAULT_GUI_FONT)
        self.SetMinSize(wx.Size(480, 360))
        self.SetSize(wx.Size(480, 720))
        self.SetTitle(self.title)
        self.SetIcon(self.icon)

        config = self.get_config()
        self.ip = config['ip']
        self.port = int(config['port'])
        self.server = (self.ip, self.port)
        self.co_driver = config['co_driver']
        self.delay = int(config['delay'])
        self.interval = int(config['interval'])
        self.volume = int(config['volume'])
        self.countdown = ast.literal_eval(config['countdown'])
        self.handbrake = config['handbrake']

        if not self.co_driver:  # First run.
            self.show_settings()
        if not self.co_driver:
            sys.exit()

        q_cfg.put_nowait((self.server, self.co_driver, self.delay-100, self.volume, self.countdown))

        self.co_path = os.path.join(app_path, 'co-drivers', self.co_driver)
        self.pace_path = os.path.join(self.co_path, 'pacenotes')
        self.sound_path = os.path.join(self.co_path, 'sounds')
        self.sound_list = defaultdict(list)
        self.sounds_csv = os.path.join(self.co_path, 'sounds.csv')

        self.dic_stages = defaultdict(list)
        self.read_stages()
        self.read_audio()

        self.file_handle = ''
        self.file_name = ''
        self.radios = []
        self.dist = 0
        self.pace = ''
        self.stage_name = ''
        self.stage_path = ''
        # self.stage_length = ''
        self.delay_mode = ''
        self.curr_dist = 0
        self.curr_line = None
        self.prev_line = None
        self.last_dist = -20
        self.cbs = set()
        self.cbs_by_id = set()
        self.checkboxes = set()
        self.line_pace = None
        self.line_pace_by_id = 0
        self.from_, self.to_ = (0, 0)
        self.sel_length = 0
        self.line_end = 0
        self.dic_lines = {}
        self.dic_entries = {}
        self.end = 0
        self.count_error = 0
        self.count_auto = 0
        self.hint = 'pacenotes'
        self.modified = False
        self.restart = False
        self.timer_error = wx.Timer(self)
        self.timer_auto = wx.Timer(self)

        self.menu_bar = MenuBar(self)

        self.menu_bar.Append(self.menu_bar.file_menu, '&File')
        self.menu_bar.Append(self.menu_bar.edit_menu, '&Edit')
        self.menu_bar.Append(self.menu_bar.autosave_menu, '&Autosave')
        self.menu_bar.Append(self.menu_bar.delay_menu, '&Pacenotes')
        self.menu_bar.Append(self.menu_bar.help_menu, '&Help')
        self.SetMenuBar(self.menu_bar)
        self.menu_bar.EnableTop(1, False)
        self.menu_bar.EnableTop(2, False)
        self.menu_bar.EnableTop(3, False)

        self.editor = Editor(self)

        self.read_sounds()
        self.reload_sounds()

        self.persist_manager = per.PersistenceManager.Get()
        config_file = os.path.join(data_path, self.editor.tabs.GetName())
        self.persist_manager.SetPersistenceFile(config_file)
        # self.persist_manager.RegisterAndRestore(self.editor.tabs)

        self.statusbar = self.CreateStatusBar()
        self.statusbar.SetName('status')
        self.statusbar.SetStatusText('Processing audio files, please wait...')

        self.taskbar = TaskBar(self)  # Create taskbar icon.

        self.reader = Reader()  # Start UDP thread.

        pub.subscribe(self.get_progress, 'get_progress')
        pub.subscribe(self.get_stage, 'get_stage')
        pub.subscribe(self.get_dist, 'get_dist')
        pub.subscribe(self.get_pause, 'get_pause')
        pub.subscribe(self.key_error, 'key_error')
        # pub.subscribe(self.get_stage_length, 'get_stage_length')

        self.progress = wx.Gauge(self.statusbar, pos=(265, 4), range=self.loaded_max)

        self.Bind(wx.EVT_TIMER, self.on_timer_error, self.timer_error)
        self.Bind(wx.EVT_TIMER, self.on_timer_auto, self.timer_auto)
        self.Bind(wx.EVT_CLOSE, self.on_quit)

        wx.CallAfter(self.register_controls)

    # Define methods.
    # Creator.
    def add_category(self, event):
        dlg = wx.TextEntryDialog(self, 'Specify a name for the new category', 'CATEGORY NAME')
        if dlg.ShowModal() == wx.ID_OK and dlg.GetValue():
            tab_right = wx.Panel(self.creator.tabs_right, style=wx.TAB_TRAVERSAL | wx.BORDER_NONE, name=dlg.GetValue(), id=2)
            tab_right.SetBackgroundColour('white')
            tab_right.SetCursor(wx.Cursor(wx.CURSOR_HAND))
            list_box_right = wx.ListBox(tab_right, style=wx.LB_MULTIPLE)
            h_box_tabs = wx.BoxSizer(wx.HORIZONTAL)
            h_box_tabs.Add(list_box_right, 0, wx.EXPAND)
            tab_right.SetSizer(h_box_tabs)
            list_box_right.Bind(wx.EVT_LISTBOX, self.on_listbox_right)
            self.creator.tabs_right.AddPage(tab_right, dlg.GetValue(), True)
        dlg.Destroy()

    def on_listbox_left(self, event):
        if event.GetExtraLong():
            self.creator.selection_left.append(event.GetString())
        else:
            self.creator.selection_left.remove(event.GetString())
        if self.creator.selection_left:
            self.creator.button_in.Enable()
        else:
            self.creator.button_in.Disable()

    def on_listbox_right(self, event):
        if event.GetExtraLong():
            self.creator.selection_right.append(event.GetString())
        else:
            self.creator.selection_right.remove(event.GetString())
        if self.creator.selection_right:
            self.creator.button_out.Enable()
        else:
            self.creator.button_out.Disable()

    def sounds_in(self, event):
        if self.creator.selection_left:
            for child_right in self.creator.tabs_right.GetCurrentPage().GetChildren():
                child_right.InsertItems(self.creator.selection_left, child_right.GetCount())
            for child_left in self.creator.tabs_left.GetCurrentPage().GetChildren():
                sel_list = child_left.GetSelections()
                sel_list.reverse()
                for selected in sel_list:
                    child_left.Delete(selected)
            self.creator.selection_left.clear()
            self.creator.button_in.Disable()
        else:
            pass

    def sounds_out(self, event):
        if self.creator.selection_right:
            for child_left in self.creator.tabs_left.GetCurrentPage().GetChildren():
                child_left.InsertItems(self.creator.selection_right, child_left.GetCount())
            for child_right in self.creator.tabs_right.GetCurrentPage().GetChildren():
                sel_list = child_right.GetSelections()
                sel_list.reverse()
                for selected in sel_list:
                    child_right.Delete(selected)
            self.creator.selection_right.clear()
            self.creator.button_out.Disable()
        else:
            pass

    def on_tab_close(self, event):
        sel_list = []
        children = self.creator.tabs_right.GetCurrentPage().GetChildren()
        for child in children:
            for row in range(child.GetCount()):
                sound = child.GetString(row)
                sel_list.append(sound)
        for child_left in self.creator.tabs_left.GetCurrentPage().GetChildren():
            child_left.InsertItems(sel_list, child_left.GetCount())

    def reset_sounds(self, event):
        self.creator.create_audio()
        self.creator.create_sounds()

    def diff(self, l_one, l_two):
        return list(set(l_one) - set(l_two))

    # DiRTy Pacenotes
    def register_controls(self):
        self.Freeze()
        self.register()
        self.Thaw()

    def register(self, children=None):
        if children is None:
            self.persist_manager.RegisterAndRestore(self)
            children = self.GetChildren()
        for child in children:
            name1 = child.GetName()
            grandchildren = child.GetChildren()
            for grandchild in grandchildren:
                name2 = grandchild.GetName()

    def on_play(self, event=None):
        snd = self.editor.input_pace.GetValue().split()
        for sound_name in snd:
            try:
                sound_pace = sound_bank[sound_name] + self.volume
                play(sound_pace)
            except KeyError:
                self.key_error(sound_name)
                pass

    def on_cancel(self, event):
        self.clear_input_pace()

    def read_audio(self):
        snd_file_list = glob.glob(self.sound_path + '/*')
        q_snd.put_nowait(snd_file_list)
        self.loaded_max = len(snd_file_list)

    def get_progress(self, arg):
        self.progress.SetValue(arg)
        if arg == self.loaded_max:
            self.progress.Destroy()
            self.SetStatusText('Open pacenotes file or start recce')

    def get_pause(self, arg):
        self.pause = arg

    def read_sounds(self):
        try:
            self.sound_list.clear()
            with open(self.sounds_csv, 'r') as csv_file:
                csv_data = csv.DictReader(csv_file)
                for row in csv_data:
                    pair = list(row.items())  # list of tuples of (key, value) pairs
                    for key, value in pair:
                        if value:
                            self.sound_list[key].append(value)  # dict with multiple values for same keys
        except IOError:
            wx.MessageBox('Create your co-driver', 'CO-DRIVER ERROR', wx.OK | wx.ICON_ERROR)
            self.show_creator()
            self.read_sounds()

    def on_creator(self, event):
        self.show_creator()

    def show_creator(self):
        self.creator = Creator(self)
        self.creator.ShowModal()

    def on_settings(self, event):
        self.show_settings()

    def show_settings(self):
        self.settings = Settings(self)
        self.settings.ShowModal()

    def read_stages(self):
        try:
            with open(os.path.join(app_path, 'data\\stages.csv'), 'r') as f:
                _ = next(f)
                for line in f:
                    row = line.strip()
                    lis = row.partition(',')  # tuple
                    key = float(lis[0])  # key as float
                    val = lis[2]  # value as string
                    self.dic_stages[key].append(val)  # dictionary
        except IOError:
            self.SetStatusText('stages.csv file not found')
            self.on_error()
        q_stg.put_nowait(self.dic_stages)

    def get_config(self):
        if not os.path.exists(config_ini):
            self.create_config(self)
        return ConfigObj(config_ini)

    @staticmethod
    def create_config(self):  # Set default values for config.ini.
        config = ConfigObj(config_ini)
        config['ip'] = '127.0.0.1'
        config['port'] = '20777'
        config['co_driver'] = ''
        config['delay'] = '200'
        config['interval'] = '1000'
        config['volume'] = '5'
        config['countdown'] = 'True'
        config['handbrake'] = 'N/A'
        config.write()

    @staticmethod
    def update_config(self):
        config = ConfigObj(config_ini)
        config['ip'] = self.ip
        config['port'] = self.port
        config['co_driver'] = self.co_driver
        config['delay'] = self.delay
        config['interval'] = self.interval
        config['volume'] = self.volume
        config['countdown'] = self.countdown
        config['handbrake'] = self.handbrake
        config.write()

    def on_change_handbrake(self, event):
        self.change_handbrake(self)

    @staticmethod
    def change_handbrake(self):
        # hand_input = HandInput()
        pass

    def get_stage(self, arg1, arg2):
        if self.stage_name:
            if arg1 != self.stage_name and self.modified:
                dlg = wx.MessageDialog(self, 'Do you want to save ' + self.file_name + '?', 'Confirm',
                                       wx.YES_NO | wx.YES_DEFAULT | wx.ICON_QUESTION)
                if dlg.ShowModal() == wx.ID_YES:
                    self.write_file()
                    wx.MessageBox(self.file_name + ' has been saved', 'Confirmation', wx.OK | wx.ICON_INFORMATION)
        self.stage_name = arg1
        self.stage_path = arg2
        self.update_stage()

    def update_stage(self):  # From UDP stream.
        self.file_name = self.stage_name + '.txt'
        self.open_file()
        self.menu_bar.EnableTop(3, True)
        self.menu_bar.menu_open.Enable(False)
        self.editor.slider_volume.Enable()
        self.editor.button_play.Enable()
        self.editor.label_delay.SetForegroundColour('dark grey')
        q_vol.put_nowait(self.volume)
        self.update_delay()

    def get_dist(self, arg1, arg2):
        self.curr_dist = arg1
        self.last_dist = arg2
        self.update_dist()

    def update_dist(self):
        if self.curr_dist >= 0 and not self.editor.input_dist.HasFocus():
            self.editor.input_dist.SetValue(self.curr_dist)

        # Manage scrolling.
        sort_keys = sorted(list(self.dic_lines.keys()), key=int)
        lines = []
        for index, d in enumerate(sort_keys):
            lines.append(index)
            if index < (len(sort_keys)):
                self.curr_line = self.dic_lines[sort_keys[index]]
                self.prev_line = self.dic_lines[sort_keys[index - 1]]
            if self.curr_dist > self.last_dist:
                if self.curr_dist == int(d) - (self.delay - 100):
                    # print self.editor.scrolled_panel.GetScrollPos(wx.VERTICAL), 'pos'
                    # print self.editor.scrolled_panel.GetScrollLines(wx.VERTICAL), 'lines'
                    self.curr_line.SetFont(self.font.Bold())
                    self.prev_line.SetFont(self.font)
                    # print index, d, 'index'  # TODO
                    if index > 1:
                        self.editor.scrolled_panel.ScrollLines(3)
                        self.Refresh()
            # elif self.curr_dist < self.last_dist:  # If going wrong way.
            #     self.curr_line.SetFont(self.font)
            #     self.prev_line.SetFont(self.font)
            #     self.Refresh()
            elif self.curr_dist == 0:  # If at the start line.
                self.editor.scrolled_panel.Scroll(0, 0)
                for l in lines:
                    self.curr_line = self.dic_lines[sort_keys[l]]
                    self.curr_line.SetFont(self.font)
                self.Refresh()
    '''
    def get_stage_length(self, arg):
        self.stage_length = arg
        self.update_length()

    def update_length(self):
        self.editor.label_length.SetLabel(str(self.stage_length))
    '''
    def on_delay(self, event):
        self.delay = event.GetId()
        q_del.put_nowait(self.delay - 100)
        self.update_delay()
        delay = self.menu_bar.delay_menu.FindItemById(self.delay).GetItemLabelText()
        self.SetStatusText('Pacenote calls set to ' + delay)

    def update_delay(self):
        if self.delay == 100:
            self.delay_mode = 'RECCE'
        else:
            self.delay_mode = 'STAGE'
        self.editor.label_delay.SetLabel(self.delay_mode)

    def key_error(self, arg):
        self.statusbar.SetStatusText('\'' + arg + '\'' + ' not found in ' + self.co_driver + '\'s Sounds folder')
        self.on_error()

    def on_error(self):
        self.statusbar.SetBackgroundColour('RED')
        self.statusbar.Refresh()
        self.timer_error.Start(50)

    def on_timer_error(self, event):
        self.count_error = self.count_error + 1
        if self.count_error == 25:
            self.statusbar.SetBackgroundColour('white')
            self.statusbar.Refresh()
            self.timer_error.Stop()
            self.count_error = 0

    def on_autosave(self):
        self.count_auto = 0
        if self.interval == 1000:
            self.SetStatusText('Autosave OFF')
        else:
            self.timer_auto.Start(60000)
            self.SetStatusText('Autosave set to ' + str(self.interval) + ' minutes')

    def on_timer_auto(self, event):
        self.count_auto = self.count_auto + 1
        if self.count_auto == self.interval:
            self.write_file()
            self.SetStatusText('File ' + self.file_name + ' has been auto-saved.')
            self.on_autosave()

    def on_interval(self, event):
        self.timer_auto.Stop()
        evt = event.GetEventObject()
        self.interval = event.GetId()
        self.on_autosave()

    def on_quit(self, event):
        if self.stage_name and self.modified:
            dlg = wx.MessageDialog(self, 'Do you want to save ' + self.file_name + '?', 'Confirm',
                                   wx.YES_NO | wx.YES_DEFAULT | wx.ICON_WARNING)
            dlg_choice = dlg.ShowModal()
            if dlg_choice == wx.ID_YES:
                self.write_file()
                dlg = wx.MessageDialog(self, self.file_name + ' has been saved', 'Confirmation',
                                       wx.OK | wx.ICON_INFORMATION)
                dlg.ShowModal()
        elif not self.stage_name:  # from 'Create your co-driver'
            pass
        self.persist_manager.SaveAndUnregister(self.editor.tabs)
        pub.unsubAll()
        udp_running = False
        q_run.put_nowait(udp_running)
        self.reader.join(0.5)
        self.update_config(self)
        self.taskbar.Destroy()
        self.Destroy()

    def on_save(self, event):
        if event.GetId() == wx.ID_SAVE:  # From menu.
            if self.checkboxes:
                if self.modified:
                    self.write_file()
                    self.SetStatusText(self.file_name + ' has been saved')
                else:
                    self.SetStatusText(self.file_name + ' has not been modified yet')
                    self.on_error()
            else:
                self.SetStatusText('There are no pacenotes to save')
                self.on_error()

    def on_reload(self, event):
        if event.GetId() == 1:  # From Creator.
            for child in self.creator.tabs_right.GetChildren():
                category = child.GetName()
                if category != 'panel':
                    for grandchild in child.GetChildren():
                        sound_list = []
                        sound_dict = {}
                        for row in range(grandchild.GetCount()):
                            sound = grandchild.GetString(row)
                            if sound:
                                sound_list.append(sound)
                        sound_list.sort()
                        sound_dict[category] = sound_list
                        self.creator.dict_list_c.update(sound_dict)
            if not self.creator.dict_list_c:
                wx.MessageBox('Create at least one category', 'CO-DRIVER ERROR', wx.OK | wx.ICON_ERROR)
            elif self.creator.dict_list_c:
                check = False
                for k, v in self.creator.dict_list_c.items():
                    if not v:
                        check = True
                if check:
                    wx.MessageBox('At least one category is empty', 'CO-DRIVER ERROR', wx.OK | wx.ICON_ERROR)
                else:
                    keys = self.creator.dict_list_c.keys()
                    with open(self.sounds_csv, 'w', newline='') as f:
                        writer = csv.writer(f, delimiter=",")
                        writer.writerow(keys)
                        writer.writerows(itertools.zip_longest(*[self.creator.dict_list_c[key] for key in keys]))
                    self.creator.Destroy()
                    self.reload_sounds()

        elif event.GetId() == 2:  # From settings.
            if self.co_driver:
                    self.ip = self.settings.ip_value.GetValue()
                    self.port = self.settings.port_value.GetValue()
                    self.co_driver = self.settings.combo_co_driver.GetValue()
                    self.countdown = self.settings.count_check.GetValue()
                    self.update_config(self)
                    self.on_quit(event)
                    self.restart_app(self)
            else:  # First run.
                self.ip = self.settings.ip_value.GetValue()
                self.port = self.settings.port_value.GetValue()
                self.co_driver = self.settings.combo_co_driver.GetValue()
                self.countdown = self.settings.count_check.GetValue()
                if not self.co_driver:
                    wx.MessageBox('Choose your co-driver', 'CO-DRIVER OPTION', wx.OK | wx.ICON_WARNING)
                    return
                self.update_config(self)
                self.settings.Destroy()

    def write_file(self):
        self.file_handle = os.path.join(self.stage_path, self.file_name)
        with open(self.file_handle, 'w') as f:
            for dist in sorted(self.dic_entries, key=int):
                pace = self.dic_entries[dist]
                line = '{},{}'.format(dist, pace)
                f.write(line + '\n')
        self.modified = False

    def on_open(self, event):
        if self.stage_name and self.checkboxes and self.modified:
            dlg = wx.MessageDialog(self, 'Do you want to save ' + self.file_name + '?', 'Confirm',
                                   wx.YES_NO | wx.YES_DEFAULT | wx.ICON_WARNING)
            dlg_choice = dlg.ShowModal()
            if dlg_choice == wx.ID_YES:
                self.on_save(event)
        dlg = wx.FileDialog(self, 'Open pacenotes file', self.pace_path, '', 'Text files (*.txt)|*.txt',
                            wx.FD_OPEN | wx.FD_FILE_MUST_EXIST)
        if dlg.ShowModal() == wx.ID_OK:
            self.file_name = dlg.GetFilename()
            self.stage_path = dlg.GetDirectory()
            self.stage_name, ext = os.path.splitext(self.file_name)
            self.open_file()
        dlg.Destroy()
        self.editor.label_delay.SetLabel('NOTES')

    def open_file(self):
        self.editor.scrolled_panel.DestroyChildren()
        self.dic_entries.clear()
        self.dic_lines.clear()
        self.SetTitle(self.title)
        file_handle = os.path.join(self.stage_path, self.file_name)
        try:
            with open(file_handle, 'r') as f:
                for line in f:
                    if line and line.split():
                        lis = line.partition(',')  # tuple
                        self.dist = int(lis[0])
                        self.pace = lis[2]
                        self.create_pacenotes()
                    else:
                        continue
        except IOError:
            self.SetStatusText(self.file_name + ' not found in ' + self.co_driver + '\'s Pacenotes folder')
            self.on_error()
            return
        self.menu_bar.menu_save.Enable(True)
        self.menu_bar.EnableTop(1, True)
        self.menu_bar.EnableTop(2, True)
        self.editor.input_pace.Clear()
        self.editor.input_pace.SetHint(self.hint)
        self.editor.tabs.Enable()
        self.editor.input_dist.Enable()
        self.editor.button_play.Disable()
        for button in self.editor.buttons:
            button.Disable()
        self.SetTitle(self.title + ' - ' + self.stage_name)
        self.modified = False
        self.on_autosave()

    def create_pacenotes(self):
        text_dist = ict.IntCtrl(self.editor.scrolled_panel, id=self.dist, name='dist', value=self.dist, min=1, max=19999,
                                size=wx.Size(45, 23), style=wx.TE_PROCESS_ENTER, limited=True, allow_none=False)
        text_pace = wx.TextCtrl(self.editor.scrolled_panel, id=self.dist, name='pace', value=self.pace)
        tick = wx.CheckBox(self.editor.scrolled_panel, id=int(self.dist), name='tick')
        text_pace.SetEditable(False)
        text_pace.SetCursor(wx.Cursor(wx.CURSOR_ARROW))

        h_box_scr = wx.BoxSizer(wx.HORIZONTAL)
        h_box_scr.Add(tick, 0, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, 3)
        h_box_scr.Add(text_dist, 0, wx.LEFT, 2)
        h_box_scr.Add(text_pace, 1, wx.EXPAND | wx.LEFT, 1)
        self.editor.v_box.Add(h_box_scr, 0, wx.EXPAND | wx.BOTTOM, 1)

        self.editor.scrolled_panel.Layout()
        self.editor.scrolled_panel.FitInside()
        self.end = self.editor.scrolled_panel.GetScrollLines(wx.VERTICAL)

        text_dist.Bind(wx.EVT_TEXT_ENTER, self.on_distance)
        text_pace.Bind(wx.EVT_MOUSE_CAPTURE_CHANGED, self.on_selection)
        self.Bind(wx.EVT_CHECKBOX, self.on_tick)

        self.dic_lines[self.dist] = text_pace
        self.dic_entries[self.dist] = self.pace.strip('\n')
        self.checkboxes.add(tick)

    def reload_pacenotes(self):
        self.editor.scrolled_panel.DestroyChildren()
        self.checkboxes.clear()
        for dist in sorted(self.dic_entries, key=int):
            self.dist = int(dist)
            self.pace = self.dic_entries[dist].strip('\n')
            self.create_pacenotes()
        q_dic.put_nowait(self.dic_entries)
        self.modified = True

    def reload_sounds(self):
        self.read_sounds()
        self.editor.tabs.DeleteAllPages()
        for category, sounds_list in list(self.sound_list.items()):
            tab = wx.Panel(self.editor.tabs, name=category)
            tab.SetCursor(wx.Cursor(wx.CURSOR_HAND))
            list_ctrl = ulc.UltimateListCtrl(tab, agwStyle=ulc.ULC_BORDER_SELECT | ulc.ULC_SORT_ASCENDING
                                                           | ulc.ULC_SINGLE_SEL | ulc.ULC_HOT_TRACKING | wx.LC_LIST)
            for index, sound in enumerate(sounds_list):
                list_ctrl.InsertStringItem(index, sound)

            h_box_tabs = wx.BoxSizer(wx.HORIZONTAL)
            h_box_tabs.Add(list_ctrl, 1, wx.EXPAND)
            tab.SetSizer(h_box_tabs)
            self.editor.tabs.AddPage(tab, category)

        self.Bind(wx.EVT_LIST_ITEM_SELECTED, self.on_pacenote)
        if not self.stage_name:
            self.editor.tabs.Disable()
        # self.editor.Refresh()

    def clear_input_pace(self):
        self.editor.input_pace.Clear()
        self.editor.input_pace.SetHint(self.hint)
        self.editor.input_dist.SetFocus()
        for button in self.editor.buttons:
            button.Disable()
        self.editor.button_play.Disable()

    def on_add(self, event):
        self.dist = self.editor.input_dist.GetValue()
        if self.stage_name:
            if self.dist != 0:
                for dist in self.dic_entries:
                    if self.dist == dist:
                        dlg = wx.MessageDialog(self, 'Replace pacenotes for current distance?', 'Confirm',
                                               wx.YES_NO | wx.NO_DEFAULT | wx.ICON_QUESTION)
                        dlg_choice = dlg.ShowModal()
                        if dlg_choice == wx.ID_YES:
                            self.add_pacenotes()
                            sort_keys = sorted(list(self.dic_lines.keys()), key=int)
                            for index, d in enumerate(sort_keys):
                                if self.dist == d:
                                    self.editor.scrolled_panel.Scroll(0, index)
                            self.SetStatusText('Pacenotes replaced')
                            return
                        elif dlg_choice == wx.ID_NO:
                            dlg.Destroy()
                            self.SetStatusText('Operation cancelled')
                            return
                self.add_pacenotes()
                sort_keys = sorted(list(self.dic_lines.keys()), key=int)
                for index, d in enumerate(sort_keys):
                    if self.dist == d:
                        self.editor.scrolled_panel.Scroll(0, index)
                self.SetStatusText('Pacenotes added')
            else:
                self.SetStatusText('Distance cannot be 0')
                self.on_error()
        else:
            self.SetStatusText('Open pacenotes text file or run a stage first')
            self.on_error()

    def add_pacenotes(self):
        self.pace = self.editor.input_pace.GetValue()
        self.dic_entries[self.dist] = self.pace.strip('\n')
        self.reload_pacenotes()
        self.editor.button_add.Disable()
        self.editor.button_insert.Disable()
        self.editor.button_replace.Disable()
        self.clear_input_pace()

    def on_insert(self, event):
        if self.sel_length == 0 == self.from_:  # Insert pacenote at the beginning of line.
            self.line_pace.SetInsertionPoint(self.from_)
        else:  # Insert pacenote after selection.
            self.line_pace.SetInsertionPoint(self.to_)
        self.line_pace.WriteText(self.editor.input_pace.GetValue())
        self.dic_entries[self.line_pace_by_id] = self.line_pace.GetValue().replace('\n', '')
        self.reload_pacenotes()
        self.editor.button_add.Disable()
        self.editor.button_insert.Disable()
        self.editor.button_replace.Disable()
        self.editor.button_delete.Disable()
        self.clear_input_pace()
        self.editor.button_play.Disable()
        sort_keys = sorted(list(self.dic_lines.keys()), key=int)
        for index, d in enumerate(sort_keys):
            if self.line_pace_by_id == int(d):
                self.editor.scrolled_panel.Scroll(0, index)
        self.SetStatusText('Pacenote inserted')

    def on_replace(self, event):
        self.line_pace.Replace(self.from_, self.to_, self.editor.input_pace.GetValue())
        self.dic_entries[self.line_pace_by_id] = self.line_pace.GetValue().strip('\n')
        self.reload_pacenotes()
        self.editor.button_add.Disable()
        self.editor.button_insert.Disable()
        self.editor.button_replace.Disable()
        self.editor.button_delete.Disable()
        self.clear_input_pace()
        self.editor.button_play.Disable()
        sort_keys = sorted(list(self.dic_lines.keys()), key=int)
        for index, d in enumerate(sort_keys):
            if self.line_pace_by_id == int(d):
                self.editor.scrolled_panel.Scroll(0, index)
        self.SetStatusText('Pacenote replaced')

    def on_delete(self, event):
        if self.cbs_by_id:  # Remove checked lines.
            for dist in self.cbs_by_id:
                del self.dic_entries[dist]
                del self.dic_lines[dist]
            self.cbs.clear()
            self.cbs_by_id.clear()
            self.checkboxes.clear()
            self.editor.button_delete.Disable()
            self.menu_bar.menu_select_all.Check(False)
        else:  # Remove selected text.
            self.line_pace.Remove(self.from_, self.to_)
            dic_2 = {}
            dic_2[self.line_pace_by_id] = self.line_pace.GetValue().strip('\n')
            self.dic_entries.update(dic_2)
        self.reload_pacenotes()
        self.clear_input_pace()
        self.editor.button_play.Disable()
        sort_keys = sorted(list(self.dic_lines.keys()), key=int)
        for index, d in enumerate(sort_keys):
            if self.line_pace_by_id == int(d):
                self.editor.scrolled_panel.Scroll(0, index)
        self.SetStatusText('Pacenote deleted')

    def on_selection(self, event):
        self.line_pace = event.GetEventObject()
        line_pace_by_name = self.line_pace.GetName()
        self.line_pace_by_id = self.line_pace.GetId()
        self.from_, self.to_ = self.line_pace.GetSelection()
        self.line_end = self.line_pace.GetLastPosition()
        self.sel_length = self.to_ - self.from_
        if self.cbs:  # Clear any ticks.
            for cb in self.cbs:
                cb.SetValue(False)
            self.cbs.clear()
            self.cbs_by_id.clear()
        else:
            if line_pace_by_name == 'pace':
                if self.sel_length > 0:
                    if self.editor.input_pace.GetValue():
                        self.editor.button_insert.Enable()
                        self.editor.button_replace.Enable()
                        self.editor.button_delete.Enable()
                    else:
                        self.editor.button_insert.Disable()
                        self.editor.button_replace.Disable()
                        self.editor.button_delete.Enable()
                elif self.sel_length == 0 == self.from_:
                    self.editor.button_insert.Enable()
                    self.editor.button_replace.Disable()
                    self.editor.button_delete.Disable()
                else:  # Prevent splitting words.
                    self.editor.button_insert.Disable()
                    self.editor.button_replace.Disable()
                    self.editor.button_delete.Disable()

    def on_distance(self, event):
        line_dist = event.GetEventObject()
        line_dist_by_name = line_dist.GetName()
        line_dist_by_id = line_dist.GetId()
        self.dist = line_dist.GetValue()
        if self.dist:
            if line_dist_by_name == 'dist':  # Processed by Enter.
                if self.dist != line_dist_by_id:
                    self.dic_entries[self.dist] = self.dic_entries.pop(line_dist_by_id, '')
                    self.dic_lines[self.dist] = self.dic_lines.pop(line_dist_by_id, '')
                    self.reload_pacenotes()
                    sort_keys = sorted(list(self.dic_lines.keys()), key=int)
                    for index, d in enumerate(sort_keys):
                        if self.line_pace_by_id == int(d):
                            self.editor.scrolled_panel.Scroll(0, index)
                    self.SetStatusText('Distance updated')
                else:
                    self.statusbar.Refresh()
            elif line_dist_by_name == 'input':
                if self.editor.input_pace.GetValue():
                    self.editor.button_add.Enable()
                    self.editor.button_play.Enable()
                else:
                    self.editor.button_add.Disable()
                    self.editor.button_play.Enable()

    def on_pacenote(self, event):
        if not self.editor.input_pace.GetValue():  # Get rid of pacenote hint.
            self.editor.input_pace.Clear()
            self.editor.button_play.Enable()
        self.editor.input_pace.AppendText(event.GetText() + ' ')
        if self.line_pace:  # If text selected.
            if self.editor.input_dist.GetValue():
                self.editor.button_add.Enable()
            self.editor.button_insert.Enable()
            self.editor.button_replace.Enable()
        elif not self.line_pace:
            if self.editor.input_dist.GetValue():
                self.editor.button_add.Enable()
                self.editor.button_insert.Disable()
                self.editor.button_replace.Disable()
            else:
                for button in self.editor.buttons:
                    button.Disable()

    def on_tick(self, event):
        self.editor.button_insert.Disable()
        self.editor.button_replace.Disable()
        cb = event.GetEventObject()
        cb_by_id = event.GetId()
        if cb_by_id != 20000:
            if cb.IsChecked():
                self.cbs.add(cb)
                self.cbs_by_id.add(cb_by_id)
                self.editor.button_delete.Enable()
                self.menu_bar.menu_select_all.IsChecked()
            else:
                self.cbs.remove(cb)
                self.cbs_by_id.remove(cb_by_id)
            if not self.cbs:
                self.editor.button_delete.Disable()
            self.menu_bar.menu_select_all.Check(False)

        else:  # Select All.
            for tick in self.checkboxes:
                tick_by_id = tick.GetId()
                if self.menu_bar.menu_select_all.IsChecked():
                    self.cbs.add(tick)
                    self.cbs_by_id.add(tick_by_id)
                    tick.SetValue(True)
                    self.editor.button_delete.Enable()
                else:
                    self.cbs.clear()
                    self.cbs_by_id.clear()
                    tick.SetValue(False)
                    self.editor.button_delete.Disable()

    def on_undo_select(self, event):
        # stock_undo = []
        # undo = self.text_pace.Undo()
        # stock_undo.append(undo)
        pass

    def on_slider(self, event):
        evt = event.GetEventObject()
        self.volume = evt.GetValue()
        q_vol.put_nowait(self.volume)

    def on_about(self, event):
        description = wordwrap('DiRTy Pacenotes lets you create your own pacenotes\n'
                               'for DiRT Rally and DiRT Rally 2.0 stages.\n'
                               'These custom pacenotes will be read by the co-driver of your choice.\n', 420,
                               wx.ClientDC(self))

        licence = wordwrap('Licensed under the Apache License, Version 2.0 (the "License");\n'
                           'you may not use this software except in compliance with the License.\n'
                           'You may obtain a copy of the License at\n'
                           'http://www.apache.org/licenses/LICENSE-2.0\n'
                           'Unless required by applicable law or agreed to in writing,\n'
                           'software distributed under the License is distributed on an "AS IS" BASIS,\n'
                           'WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,\n'
                           'either express or implied. See the License for the specific language\n'
                           'governing permissions and limitations under the License.', 420, wx.ClientDC(self))
        icon = wx.Icon(os.path.join(img_path, 'icon.png'))

        info = wx.adv.AboutDialogInfo()

        info.SetName('DiRTy Pacenotes')
        info.SetVersion('2.5.1')
        info.SetIcon(icon)
        info.SetDescription(description)
        info.SetCopyright('(C) 2017 - 2019 Palo Samo')
        info.SetLicence(licence)
        wx.adv.AboutBox(info)

    @staticmethod
    def restart_app(self):
        sys.stdout.flush()
        os.execl(sys.executable, sys.executable, *sys.argv)


if __name__ == '__main__':
    app = wx.App()
    frame = DiRTyPacenotes(None)
    frame.Centre()
    frame.Show()
    app.MainLoop()
