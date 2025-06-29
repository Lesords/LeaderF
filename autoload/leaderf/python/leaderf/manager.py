#!/usr/bin/env python
# -*- coding: utf-8 -*-

import vim
import os
import sys
import json
import time
import operator
import itertools
import threading
import multiprocessing
from functools import partial
from functools import wraps
from .instance import LfInstance
from .cli import LfCli
from .utils import *
from .fuzzyMatch import FuzzyMatch
from .asyncExecutor import AsyncExecutor
from .devicons import (
    webDevIconsGetFileTypeSymbol,
    removeDevIcons
)

is_fuzzyEngine_C = False
try:
    import fuzzyEngine
    is_fuzzyEngine_C = True
    cpu_count = multiprocessing.cpu_count()
    lfCmd("let g:Lf_fuzzyEngine_C = 1")
except ImportError:
    lfCmd("let g:Lf_fuzzyEngine_C = 0")

is_fuzzyMatch_C = False
try:
    import fuzzyMatchC
    is_fuzzyMatch_C = True
    lfCmd("let g:Lf_fuzzyMatch_C = 1")
except ImportError:
    lfCmd("let g:Lf_fuzzyMatch_C = 0")

if sys.version_info >= (3, 0):
    def isAscii(str):
        try:
            str.encode("ascii")
            return True
        except UnicodeEncodeError:
            return False
else:
    def isAscii(str):
        try:
            str.decode("ascii")
            return True
        except UnicodeDecodeError:
            return False


def modifiableController(func):
    @wraps(func)
    def deco(self, *args, **kwargs):
        self._getInstance().buffer.options['modifiable'] = True
        func(self, *args, **kwargs)
        try:
            self._getInstance().buffer.options['modifiable'] = False
        except:
            pass
    return deco

def catchException(func):
    @wraps(func)
    def deco(self, *args, **kwargs):
        try:
            return func(self, *args, **kwargs)
        except vim.error as e: # for neovim
            if str(e) != "b'Keyboard interrupt'" and str(e) != 'Keyboard interrupt':
                raise e
            elif self._timer_id is not None:
                lfCmd("call timer_stop(%s)" % self._timer_id)
                self._timer_id = None
        except KeyboardInterrupt: # <C-C>, this does not work in vim
            if self._timer_id is not None:
                lfCmd("call timer_stop(%s)" % self._timer_id)
                self._timer_id = None
    return deco

def windo(func):
    if lfEval("has('nvim')") == '0':
        return func

    @wraps(func)
    def deco(self, *args, **kwargs):
        try:
            cur_winid = lfEval("win_getid()")
            lfCmd("noautocmd call win_gotoid(%d)" % self._getInstance().windowId)
            return func(self, *args, **kwargs)
        finally:
            lfCmd("noautocmd call win_gotoid(%s)" % cur_winid)

    return deco

#*****************************************************
# Manager
#*****************************************************
class Manager(object):
    def __init__(self):
        self._autochdir = 0
        self._cli = LfCli()
        self._explorer = None
        self._instance = None
        self._content = []
        self._index = 0
        self._help_length = 0
        self._show_help = False
        self._selections = {}
        self._highlight_pos = []
        self._highlight_pos_list = []
        self._highlight_refine_pos = []
        self._highlight_ids = []
        self._orig_line = None
        self._fuzzy_engine = None
        self._result_content = []
        self._reader_thread = None
        self._timer_id = None
        self._highlight_method = lambda : None
        self._orig_cwd = None
        self._cursorline_dict = {}
        self._empty_query = lfEval("get(g:, 'Lf_EmptyQuery', 1)") == '1'
        self._preview_winid = 0
        self._is_previewed = False
        self._match_ids = []
        self._vim_file_autoloaded = False
        self._arguments = {}
        self._getExplClass()
        self._preview_filetype = None
        self._orig_source = None
        self._preview_config = {}
        self.is_autocmd = False
        self.is_ctrl_c = False
        self._circular_scroll = lfEval("get(g:, 'Lf_EnableCircularScroll', 0)") == '1'
        if lfEval("has('patch-8.1.1615') || has('nvim-0.5.0')") == '0':
            lfCmd("let g:Lf_PreviewInPopup = 0")

    #**************************************************************
    # abstract methods, in fact all the functions can be overridden
    #**************************************************************
    def _getExplClass(self):
        """
        this function MUST be overridden
        return the name of Explorer class
        """
        raise NotImplementedError("Can't instantiate abstract class Manager "
                                  "with abstract methods _getExplClass")

    def _defineMaps(self):
        pass

    def _defineNormalCommandMaps(self):
        normal_map = lfEval("get(g:, 'Lf_NormalCommandMap', {})")
        if not normal_map:
            return

        command_map = normal_map.get("*", {})
        command_map.update(normal_map.get(self._getExplorer().getStlCategory(), {}))
        new_commands = set(i.lower() if i.startswith('<') else i for i in command_map.values())
        map_rhs = {k: lfEval("maparg('{}', 'n', 0, 1)".format(k)) for k in command_map}
        for old, new in command_map.items():
            maparg = map_rhs[old]
            if maparg and maparg["buffer"] == '1':
                lfCmd("silent! nnoremap <buffer> <silent> {} {}".format(new, maparg["rhs"]))
                old_cmd = old.lower() if old.startswith('<') else old
                if old_cmd not in new_commands:
                    lfCmd("silent! nunmap <buffer> {}".format(old))

    def _defineCommonMaps(self):
        normal_map = lfEval("get(g:, 'Lf_NormalMap', {})")
        if "_" not in normal_map:
            return

        for [lhs, rhs] in normal_map["_"]:
            # If a buffer-local mapping does not exist, map it
            maparg = lfEval("maparg('{}', 'n', 0, 1)".format(lhs))
            if maparg == {} or maparg.get("buffer", "0") == "0" :
                lfCmd("nnoremap <buffer> <silent> {} {}".format(lhs, rhs))

    def _cmdExtension(self, cmd):
        """
        this function can be overridden to add new cmd
        if return true, exit the input loop
        """
        pass

    @removeDevIcons
    def _argaddFiles(self, files):
        # simply delete all, without err print
        lfCmd("%argdelete")
        for file in files:
            if not os.path.isabs(file):
                if self._getExplorer()._cmd_work_dir:
                    file = os.path.join(self._getExplorer()._cmd_work_dir, lfDecode(file))
                else:
                    file = os.path.join(self._getInstance().getCwd(), lfDecode(file))
                file = os.path.normpath(lfEncode(file))

            lfCmd("argadd %s" % escSpecial(file))

    def _issue_422_set_option(self):
        if lfEval("has('nvim')") == '1' and self._is_previewed:
            lfCmd("silent! setlocal number<")
            lfCmd("silent! setlocal relativenumber<")
            lfCmd("silent! setlocal cursorline<")
            lfCmd("silent! setlocal colorcolumn<")
            lfCmd("silent! setlocal winhighlight<")

    def _acceptSelection(self, *args, **kwargs):
        pass

    def autoJump(self, content):
        return False

    def _getDigest(self, line, mode):
        """
        this function can be overridden
        specify what part in the line to be processed and highlighted
        Args:
            mode: 0, return the full path
                  1, return the name only
                  2, return the directory name
        """
        if mode == 0:
            return line
        elif mode == 1:
            return getBasename(line)
        else:
            return getDirname(line)

    def _getDigestStartPos(self, line, mode):
        """
        this function can be overridden
        return the start position of the digest returned by _getDigest()
        Args:
            mode: 0, return the start postion of full path
                  1, return the start postion of name only
                  2, return the start postion of directory name
        """
        if mode == 0 or mode == 2:
            return 0
        else:
            return lfBytesLen(getDirname(line))

    def _createHelp(self):
        return []

    def _setStlMode(self, the_mode=None, **kwargs):
        if self._cli.isFuzzy:
            if self._getExplorer().supportsNameOnly():
                if self._cli.isFullPath:
                    mode = 'FullPath'
                else:
                    mode = 'NameOnly'
            else:
                mode = 'Fuzzy'
        else:
            mode = 'Regex'

        modes = {"--nameOnly", "--fullPath", "--fuzzy", "--regexMode"}
        for opt in kwargs.get("arguments", {}):
            if opt in modes:
                if opt == "--regexMode":
                    mode = 'Regex'
                elif self._getExplorer().supportsNameOnly():
                    if opt == "--nameOnly":
                        mode = 'NameOnly'
                    elif opt == "--fullPath":
                        mode = 'FullPath'
                    else: # "--fuzzy"
                        if self._cli.isFullPath:
                            mode = 'FullPath'
                        else:
                            mode = 'NameOnly'
                elif opt in ("--nameOnly", "--fullPath", "--fuzzy"):
                    mode = 'Fuzzy'

                break

        if the_mode is not None:
            mode = the_mode
        elif self._cli._is_live:
            mode = 'Fuzzy'

        self.setStlMode(mode)
        self._cli.setCurrentMode(mode)

    def setStlMode(self, mode):
        self._getInstance().setStlMode(mode)

    def _beforeEnter(self):
        self._resetAutochdir()
        self._cur_buffer = vim.current.buffer
        self._laststatus = lfEval("&laststatus")
        if self._laststatus == '0':
            lfCmd("set laststatus=2")

    def _afterEnter(self):
        if self._vim_file_autoloaded == False:
            category = self._getExplorer().getStlCategory()
            if category == 'Colorscheme':
                category = 'Colors'
            lfCmd("silent! call leaderf#%s#a_nonexistent_function()" % category)
            self._vim_file_autoloaded = True

        if "--nowrap" in self._arguments:
            if self._getInstance().getWinPos() == 'popup':
                lfCmd("call win_execute(%d, 'setlocal nowrap')" % self._getInstance().getPopupWinId())
            elif self._getInstance().getWinPos() == 'floatwin':
                lfCmd("call nvim_win_set_option(%d, 'wrap', v:false)" % self._getInstance().getPopupWinId())
            else:
                self._getInstance().window.options['wrap'] = False
        else:
            if self._getInstance().getWinPos() == 'popup':
                lfCmd("call win_execute(%d, 'setlocal wrap')" % self._getInstance().getPopupWinId())
            elif self._getInstance().getWinPos() == 'floatwin':
                lfCmd("call nvim_win_set_option(%d, 'wrap', v:true)" % self._getInstance().getPopupWinId())
            else:
                self._getInstance().window.options['wrap'] = True

        if self._getInstance().getWinPos() != 'popup':
            self._defineMaps()
            self._defineCommonMaps()
            self._defineNormalCommandMaps()

            id = int(lfEval(r"matchadd('Lf_hl_cursorline', '.*\%#.*', -100)"))
            self._match_ids.append(id)
        else:
            lfCmd(r"""call win_execute({}, 'let matchid = matchadd(''Lf_hl_cursorline'', ''.*\%#.*'', -100)')"""
                    .format(self._getInstance().getPopupWinId()))
            id = int(lfEval("matchid"))
            self._match_ids.append(id)

        if is_fuzzyEngine_C:
            self._fuzzy_engine = fuzzyEngine.createFuzzyEngine(cpu_count, False)

    def _beforeExit(self):
        if self._getInstance().window.valid:
            self._getInstance().cursorRow = self._getInstance().window.cursor[0]
        self._getInstance().helpLength = self._help_length
        self.clearSelections()
        self._getExplorer().cleanup()
        if self._fuzzy_engine:
            fuzzyEngine.closeFuzzyEngine(self._fuzzy_engine)
            self._fuzzy_engine = None

        if self._reader_thread and self._reader_thread.is_alive():
            self._stop_reader_thread = True

        self._closePreviewPopup()

        if self._getInstance().getWinPos() == 'popup':
            for i in self._match_ids:
                lfCmd("silent! call matchdelete(%d, %d)" % (i, self._getInstance().getPopupWinId()))
        else:
            for i in self._match_ids:
                lfCmd("silent! call matchdelete(%d)" % i)
        self._match_ids = []

    def _afterExit(self):
        if self._laststatus == '0':
            lfCmd("set laststatus=%s" % self._laststatus)

    def _bangEnter(self):
        self._current_mode = 'NORMAL'
        if self._getInstance().getWinPos() == 'popup':
            self._cli.hideCursor()
            if lfEval("exists('*leaderf#%s#NormalModeFilter')" % self._getExplorer().getStlCategory()) == '1':
                lfCmd("call leaderf#ResetPopupOptions(%d, 'filter', '%s')" % (self._getInstance().getPopupWinId(),
                        'leaderf#%s#NormalModeFilter' % self._getExplorer().getStlCategory()))
            else:
                lfCmd("call leaderf#ResetPopupOptions(%d, 'filter', function('leaderf#NormalModeFilter', [%d]))"
                        % (self._getInstance().getPopupWinId(), id(self)))

        self._resetHighlights()
        if self._cli.pattern and self._index == 0:
            self._search(self._content)
            if len(self._getInstance().buffer) < len(self._result_content):
                self._getInstance().appendBuffer(self._result_content[self._initial_count:])

    def _bangReadFinished(self):
        pass

    def _getList(self, pairs):
        """
        this function can be overridden
        return a list constructed from pairs
        Args:
            pairs: a list of tuple(weight, line, ...)
        """
        return [p[1] for p in pairs]

    def _getUnit(self):
        """
        indicates how many lines are considered as a unit
        """
        return 1

    def _supportsRefine(self):
        return False

    def _previewInPopup(self, *args, **kwargs):
        pass

    def closePreviewPopupOrQuit(self):
        if self._getInstance().getWinPos() in ('popup', 'floatwin'):
            self.quit()
        elif self._preview_winid:
            self._closePreviewPopup()
        else:
            self.quit()

    def _closePreviewPopup(self):
        if lfEval("has('nvim')") == '1':
            if self._preview_winid:
                if int(lfEval("nvim_win_is_valid(%d) == v:true" % self._preview_winid)):
                    lfCmd("call nvim_win_close(%d, 1)" % self._preview_winid)
                self._preview_winid = 0
        else:
            if self._preview_winid:
                lfCmd("call popup_close(%d)" % self._preview_winid)
                self._preview_winid = 0
                lfCmd("silent! noautocmd bwipe /Lf_preview_{}".format(id(self)))

        self._preview_filetype = None

    def _previewResult(self, preview):
        preview_in_popup = (lfEval("get(g:, 'Lf_PreviewInPopup', 1)") == '1'
                            or self._getInstance().getWinPos() in ('popup', 'floatwin'))

        if preview == False and self._orig_line == self._getInstance().currentLine:
            return

        self._orig_line = self._getInstance().currentLine

        if not self._needPreview(preview, preview_in_popup):
            if preview_in_popup:
                self._closePreviewPopup()
                self._getInstance().enlargePopupWindow()
            return

        line_num = self._getInstance().window.cursor[0]
        line = self._getInstance().buffer[line_num - 1]

        if preview_in_popup:
            self._previewInPopup(line, self._getInstance().buffer, line_num)

            if not self.isPreviewWindowOpen():
                self._getInstance().enlargePopupWindow()

            return

        orig_pos = self._getInstance().getOriginalPos()
        cur_pos = (vim.current.tabpage, vim.current.window, vim.current.buffer)

        saved_eventignore = vim.options['eventignore']
        vim.options['eventignore'] = 'BufLeave,WinEnter,BufEnter'
        try:
            vim.current.tabpage, vim.current.window = orig_pos[:2]
            line_num = self._getInstance().window.cursor[0]
            self._acceptSelection(line, self._getInstance().buffer, line_num, preview=True)
            lfCmd("augroup Lf_Cursorline")
            lfCmd("autocmd! BufwinEnter <buffer> setlocal cursorline<")
            lfCmd("augroup END")
        finally:
            if self._getInstance().getWinPos() != 'popup':
                vim.current.tabpage, vim.current.window, vim.current.buffer = cur_pos
            vim.options['eventignore'] = saved_eventignore

    def _restoreOrigCwd(self):
        if self._orig_cwd is None:
            return

        # https://github.com/neovim/neovim/issues/8336
        if lfEval("has('nvim')") == '1':
            chdir = vim.chdir
        else:
            chdir = os.chdir

        try:
            if int(lfEval("&autochdir")) == 0 and lfGetCwd() != self._orig_cwd:
                chdir(self._orig_cwd)
        except:
            if lfGetCwd() != self._orig_cwd:
                chdir(self._orig_cwd)

    def _needExit(self, line, arguments):
        return True

    def setArguments(self, arguments):
        self._arguments = arguments

    def getArguments(self):
        return self._arguments

    #**************************************************************

    def _setWinOptions(self, winid):
        if lfEval("has('nvim')") == '1':
            lfCmd("call nvim_win_set_option(%d, 'number', v:true)" % winid)
            lfCmd("call nvim_win_set_option(%d, 'relativenumber', v:false)" % winid)
            lfCmd("call nvim_win_set_option(%d, 'cursorline', v:true)" % winid)
            lfCmd("call nvim_win_set_option(%d, 'foldenable', v:false)" % winid)
            lfCmd("call nvim_win_set_option(%d, 'foldmethod', 'manual')" % winid)
            lfCmd("call nvim_win_set_option(%d, 'foldcolumn', '0')" % winid)
            lfCmd("call nvim_win_set_option(%d, 'signcolumn', 'no')" % winid)
            if lfEval("exists('+cursorlineopt')") == '1':
                lfCmd("call nvim_win_set_option(%d, 'cursorlineopt', 'both')" % winid)
            lfCmd("call nvim_win_set_option(%d, 'colorcolumn', '')" % winid)
            lfCmd("call nvim_win_set_option(%d, 'winhighlight', 'Normal:Lf_hl_popup_window')" % winid)
        else:
            lfCmd("noautocmd call win_execute(%d, 'setlocal number norelativenumber cursorline')" % winid)
            lfCmd("noautocmd call win_execute(%d, 'setlocal nofoldenable foldmethod=manual')" % winid)
            if lfEval("get(g:, 'Lf_PopupShowFoldcolumn', 1)") == '0' or lfEval("get(g:, 'Lf_PopupShowBorder', 1)") == '1':
                lfCmd("call win_execute(%d, 'setlocal foldcolumn=0')" % winid)
            else:
                lfCmd("call win_execute(%d, 'setlocal foldcolumn=1')" % winid)
            if lfEval("exists('+cursorlineopt')") == '1':
                lfCmd("call win_execute(%d, 'setlocal cursorlineopt=both')" % winid)
            lfCmd("call win_execute(%d, 'setlocal colorcolumn=')" % winid)
            lfCmd("call win_execute(%d, 'setlocal wincolor=Lf_hl_popup_window')" % winid)

    def _createPreviewWindow(self, config, source, line_num, jump_cmd):
        self._preview_config = config
        self._orig_source = source

        if lfEval("has('nvim')") == '1':
            if isinstance(source, int):
                buffer_len = len(vim.buffers[source])
                self._preview_winid = int(lfEval("nvim_open_win(%d, 0, %s)" % (source, str(config))))
            else:
                try:
                    if self._isBinaryFile(source):
                        lfCmd("""let content = map(range(128), '"^@"')""")
                    else:
                        lfCmd("let content = readfile('%s', '', 20480)" % escQuote(source))
                except vim.error as e:
                    lfPrintError(e)
                    return
                buffer_len = int(lfEval("len(content)"))
                lfCmd("noautocmd let g:Lf_preview_scratch_buffer = nvim_create_buf(0, 1)")
                lfCmd("noautocmd call setbufline(g:Lf_preview_scratch_buffer, 1, content)")
                lfCmd("noautocmd call nvim_buf_set_option(g:Lf_preview_scratch_buffer, 'bufhidden', 'wipe')")
                lfCmd("noautocmd call nvim_buf_set_option(g:Lf_preview_scratch_buffer, 'undolevels', -1)")
                lfCmd("noautocmd call nvim_buf_set_option(g:Lf_preview_scratch_buffer, 'modeline', v:true)")

                self._preview_winid = int(lfEval("nvim_open_win(g:Lf_preview_scratch_buffer, 0, %s)" % str(config)))

            cur_winid = lfEval("win_getid()")
            lfCmd("noautocmd call win_gotoid(%d)" % self._preview_winid)
            if not isinstance(source, int):
                file_type = getExtension(source)
                if file_type is None:
                    lfCmd("silent! doautocmd filetypedetect BufNewFile %s" % source)
                else:
                    lfCmd("noautocmd set ft=%s" % getExtension(source))
                    lfCmd("set syntax=%s" % getExtension(source))
            lfCmd("noautocmd call win_gotoid(%s)" % cur_winid)

            self._setWinOptions(self._preview_winid)
            self._preview_filetype = lfEval("getbufvar(winbufnr(%d), '&ft')" % self._preview_winid)

            lfCmd("noautocmd call win_gotoid(%d)" % self._preview_winid)
            if jump_cmd:
                lfCmd(jump_cmd)
            if buffer_len >= line_num > 0:
                lfCmd("""call nvim_win_set_cursor(%d, [%d, 1])""" % (self._preview_winid, line_num))
            lfCmd("norm! zz")
            lfCmd("noautocmd call win_gotoid(%s)" % cur_winid)
        elif lfEval("exists('*popup_setbuf')") == "1":
            if isinstance(source, int):
                lfCmd("noautocmd silent! let winid = popup_create(%d, %s)"
                      % (source, json.dumps(config)))
            else:
                filename = source
                try:
                    if self._isBinaryFile(filename):
                        lfCmd("""let content = map(range(128), '"^@"')""")
                    else:
                        lfCmd("let content = readfile('%s', '', 20480)" % escQuote(filename))
                except vim.error as e:
                    lfPrintError(e)
                    return

                lfCmd("noautocmd silent! let winid = popup_create(bufadd('/Lf_preview_%s'), %s)"
                      % (id(self), json.dumps(config)))
                lfCmd("call win_execute(winid, 'setlocal modeline')")
                lfCmd("call win_execute(winid, 'setlocal undolevels=-1')")
                lfCmd("call win_execute(winid, 'setlocal noswapfile')")
                lfCmd("call win_execute(winid, 'setlocal nobuflisted')")
                lfCmd("call win_execute(winid, 'setlocal bufhidden=hide')")
                lfCmd("call win_execute(winid, 'setlocal buftype=nofile')")
                lfCmd("noautocmd call popup_settext(winid, content)")

                lfCmd("call win_execute(winid, 'silent! doautocmd filetypedetect BufNewFile %s')"
                      % escQuote(filename))

            self._preview_winid = int(lfEval("winid"))
            self._setWinOptions(self._preview_winid)
            self._preview_filetype = lfEval("getbufvar(winbufnr(winid), '&ft')")

            if jump_cmd:
                lfCmd("""call win_execute(%d, '%s')""" % (self._preview_winid, escQuote(jump_cmd)))
                lfCmd("call win_execute(%d, 'norm! zz')" % self._preview_winid)
            elif line_num > 0:
                lfCmd("""call win_execute(%d, "call cursor(%d, 1)")""" % (self._preview_winid, line_num))
                lfCmd("call win_execute(%d, 'norm! zz')" % self._preview_winid)
        else:
            if isinstance(source, int):
                lfCmd("let content = getbufline(%d, 1, '$')" % source)
                filename = vim.buffers[source].name
            else:
                filename = source
                try:
                    if self._isBinaryFile(filename):
                        lfCmd("""let content = map(range(128), '"^@"')""")
                    else:
                        lfCmd("let content = readfile('%s', '', 20480)" % escQuote(filename))
                except vim.error as e:
                    lfPrintError(e)
                    return

            lfCmd("noautocmd silent! let winid = popup_create(content, %s)" % json.dumps(config))
            lfCmd("call win_execute(winid, 'setlocal modeline')")

            lfCmd("call win_execute(winid, 'silent! doautocmd filetypedetect BufNewFile %s')" % escQuote(filename))

            self._preview_winid = int(lfEval("winid"))
            self._setWinOptions(self._preview_winid)
            self._preview_filetype = lfEval("getbufvar(winbufnr(winid), '&ft')")

            if jump_cmd:
                lfCmd("""call win_execute(%d, '%s')""" % (self._preview_winid, escQuote(jump_cmd)))
                lfCmd("call win_execute(%d, 'norm! zz')" % self._preview_winid)
            elif line_num > 0:
                lfCmd("""call win_execute(%d, "call cursor(%d, 1)")""" % (self._preview_winid, line_num))
                lfCmd("call win_execute(%d, 'norm! zz')" % self._preview_winid)

    @ignoreEvent('BufWinEnter,BufEnter')
    def _createPopupModePreview(self, title, source, line_num, jump_cmd):
        """
        Args:
            source:
                if the type is int, it is a buffer number
                if the type is str, it is a file name

        """
        self._getInstance().shrinkPopupWindow()

        self._is_previewed = True

        show_borders = lfEval("get(g:, 'Lf_PopupShowBorder', 1)") == '1'
        preview_pos = self._arguments.get("--preview-position", [""])[0]
        if preview_pos == "":
            preview_pos = lfEval("get(g:, 'Lf_PopupPreviewPosition', 'right')")

        if lfEval("has('nvim')") == '1':
            width = int(lfEval("get(g:, 'Lf_PreviewPopupWidth', 0)"))
            if width <= 0:
                maxwidth = int(lfEval("&columns"))//2
            else:
                maxwidth = min(width, int(lfEval("&columns")))

            float_window = self._getInstance().window
            # row and col start from 0
            float_win_row = int(float(lfEval("nvim_win_get_config(%d).row" % float_window.id)))
            float_win_col = int(float(lfEval("nvim_win_get_config(%d).col" % float_window.id)))
            float_win_height = int(float(lfEval("nvim_win_get_config(%d).height" % float_window.id)))
            float_win_width= int(float(lfEval("nvim_win_get_config(%d).width" % float_window.id)))
            popup_borders = lfEval("g:Lf_PopupBorders")
            borderchars = [
                    [popup_borders[4],  "Lf_hl_popupBorder"],
                    [popup_borders[0],  "Lf_hl_popupBorder"],
                    [popup_borders[5],  "Lf_hl_popupBorder"],
                    [popup_borders[1],  "Lf_hl_popupBorder"],
                    [popup_borders[6],  "Lf_hl_popupBorder"],
                    [popup_borders[2],  "Lf_hl_popupBorder"],
                    [popup_borders[7],  "Lf_hl_popupBorder"],
                    [popup_borders[3],  "Lf_hl_popupBorder"]
                    ]

            relative = 'editor'
            if preview_pos.lower() == 'bottom':
                anchor = "NW"
                if self._getInstance().getPopupInstance().statusline_win:
                    statusline_height = 1
                else:
                    statusline_height = 0
                row = float_win_row + float_window.height + statusline_height
                col = float_win_col
                height = int(lfEval("&lines")) - row - 3
                if height < 1:
                    return
                width = float_window.width
                borderchars[0] = ''
                borderchars[1] = ''
                borderchars[2] = ''
            elif preview_pos.lower() == 'top':
                anchor = "SW"
                row = float_win_row - 1
                if show_borders:
                    row -= 1
                col = float_win_col
                height = row
                if height < 1:
                    return
                width = float_window.width
                borderchars[4] = ''
                borderchars[5] = ''
                borderchars[6] = ''
            elif preview_pos.lower() == 'right':
                anchor = "NW"
                row = float_win_row - 1
                col = float_win_col + float_win_width
                if show_borders:
                    row -= 1
                    col += 2
                height = self._getInstance().getPopupHeight() + 1
                if width <= 0:
                    width = float_win_width
                if show_borders:
                    width = min(width, int(lfEval("&columns")) - col - 2)
                else:
                    width = min(width, int(lfEval("&columns")) - col)
            elif preview_pos.lower() == 'left':
                anchor = "NE"
                row = float_win_row - 1
                col = float_win_col
                if show_borders:
                    row -= 1
                height = self._getInstance().getPopupHeight() + 1
                if width <= 0:
                    width = float_win_width
                width = min(width, col)
            else:
                start = int(lfEval("line('w0')")) - 1
                end = int(lfEval("line('.')")) - 1
                col_width = float_window.width - int(lfEval("&numberwidth")) - 1
                delta_height = lfActualLineCount(self._getInstance().buffer, start, end, col_width)
                win_height = int(lfEval("&lines"))
                if float_win_row + delta_height < win_height // 2:
                    anchor = "NW"
                    row = float_win_row + delta_height + 1
                    height = win_height - int(lfEval("&cmdheight")) - row
                else:
                    anchor = "SW"
                    row = float_win_row + delta_height
                    height = row
                col = float_win_col + int(lfEval("&numberwidth")) + 1 + float_window.cursor[1]
                width = maxwidth

            config = {
                    "relative": relative,
                    "anchor"  : anchor,
                    "height"  : height,
                    "width"   : width,
                    "zindex"  : 20481,
                    "row"     : row,
                    "col"     : col,
                    "noautocmd": 1
                    }

            if show_borders:
                config["border"] = borderchars
                if lfEval("has('nvim-0.9.0')") == '1':
                    config["title"] = " Preview "
                    config["title_pos"] = "center"

            self._createPreviewWindow(config, source, line_num, jump_cmd)
            lfCmd("let g:Lf_PreviewWindowID[%d] = %d" % (id(self), self._preview_winid))
        else:
            popup_window = self._getInstance().window
            popup_pos = lfEval("popup_getpos(%d)" % popup_window.id)

            width = int(lfEval("get(g:, 'Lf_PreviewPopupWidth', 0)"))
            if width <= 0:
                maxwidth = int(lfEval("&columns"))//2 - 1
            else:
                maxwidth = min(width, int(lfEval("&columns")))

            if preview_pos.lower() == 'bottom':
                maxwidth = int(popup_pos["width"])
                col = int(popup_pos["col"])
                if self._getInstance().getPopupInstance().statusline_win:
                    statusline_height = 1
                else:
                    statusline_height = 0
                line = int(popup_pos["line"]) + int(popup_pos["height"]) + statusline_height
                pos = "topleft"
                maxheight = int(lfEval("&lines")) - line - 2
                if maxheight < 1:
                    return

            elif preview_pos.lower() == 'top':
                maxwidth = int(popup_pos["width"])
                col = int(popup_pos["col"])
                # int(popup_pos["line"]) - 1(exclude the first line) - 1(input window) - 1(title)
                maxheight = int(popup_pos["line"]) - 3
                if maxheight < 1:
                    return

                pos = "botleft"
                line = maxheight + 1
            elif preview_pos.lower() == 'right':
                col = int(popup_pos["col"]) + int(popup_pos["width"])
                line = int(popup_pos["line"]) - 1
                maxheight = self._getInstance().getPopupHeight()
                pos = "topleft"
                if width == 0:
                    maxwidth = int(popup_pos["width"])
                maxwidth = min(maxwidth, int(lfEval("&columns")) - col + 1)
            elif preview_pos.lower() == 'left':
                col = int(popup_pos["col"]) - 1
                line = int(popup_pos["line"]) - 1
                maxheight = self._getInstance().getPopupHeight()
                pos = "topright"
                if width == 0:
                    maxwidth = int(popup_pos["width"])
                maxwidth = min(maxwidth, col)
            else: # cursor
                lfCmd("""call win_execute(%d, "let numberwidth = &numberwidth")""" % popup_window.id)
                col = int(popup_pos["core_col"]) + int(lfEval("numberwidth")) + popup_window.cursor[1]

                lfCmd("""call win_execute(%d, "let delta_height = line('.') - line('w0')")""" % popup_window.id)
                # the line of buffer starts from 0, while the line of line() starts from 1
                start = int(lfEval("line('w0', %d)" % popup_window.id)) - 1
                end = int(lfEval("line('.', %d)" % popup_window.id)) - 1
                col_width = int(popup_pos["core_width"]) - int(lfEval("numberwidth"))
                delta_height = lfActualLineCount(self._getInstance().buffer, start, end, col_width)
                # int(popup_pos["core_line"]) - 1(exclude the first line) - 1(input window)
                maxheight = int(popup_pos["core_line"]) + delta_height - 2
                pos = "botleft"
                line = maxheight + 1

            options = {
                    "title":           " Preview ",
                    "maxwidth":        maxwidth,
                    "minwidth":        maxwidth,
                    "maxheight":       maxheight,
                    "minheight":       maxheight,
                    "zindex":          20481,
                    "pos":             pos,
                    "line":            line,
                    "col":             col,
                    "scrollbar":       0,
                    "padding":         [0, 0, 0, 0],
                    "border":          [1, 0, 0, 0],
                    "borderchars":     [' '],
                    "borderhighlight": ["Lf_hl_previewTitle"],
                    "filter":          "leaderf#popupModePreviewFilter",
                    }

            if show_borders:
                options["borderchars"] = lfEval("g:Lf_PopupBorders")
                options["maxwidth"] -= 2
                options["minwidth"] -= 2
                options["borderhighlight"] = ["Lf_hl_popupBorder"]

            if preview_pos.lower() == 'bottom':
                del options["title"]
                options["border"] = [0, 0, 1, 0]
                if show_borders:
                    options["border"] = [0, 1, 1, 1]
            elif preview_pos.lower() == 'top':
                if show_borders:
                    options["border"] = [1, 1, 0, 1]
            elif preview_pos.lower() == 'right':
                if show_borders:
                    options["border"] = [1, 1, 1, 1]
                    options["line"] -= 1
                    # options["col"] += 1
                    options["maxheight"] += 1
                    options["minheight"] += 1
            elif preview_pos.lower() == 'left':
                if show_borders:
                    options["border"] = [1, 1, 1, 1]
                    options["line"] -= 1
                    # options["col"] -= 1
                    options["maxheight"] += 1
                    options["minheight"] += 1
            elif preview_pos.lower() == 'cursor' and maxheight < int(lfEval("&lines"))//2 - 2:
                maxheight = int(lfEval("&lines")) - maxheight - 5
                del options["title"]
                options["border"] = [0, 0, 1, 0]
                options["maxheight"] = maxheight
                options["minheight"] = maxheight

            self._createPreviewWindow(options, source, line_num, jump_cmd)


    def isPreviewWindowOpen(self):
        return self._preview_winid > 0 and int(lfEval("winbufnr(%d)" % self._preview_winid)) != -1

    def _isBinaryFile(self, filename):
        try:
            is_binary = False
            with lfOpen(filename, 'r', encoding='utf-8', errors='ignore') as f:
                data = f.read(128)
                for i in data:
                    if i == '\0':
                        is_binary = True
                        break

            return is_binary
        except Exception as e:
            lfPrintError(e)
            return True

    def setOptionsForCursor(self):
        preview_pos = self._arguments.get("--preview-position", [""])[0]
        if preview_pos == "":
            preview_pos = lfEval("get(g:, 'Lf_PreviewPosition', 'top')")

        if preview_pos == "cursor" and self._getInstance().getWinPos() not in ('popup', 'floatwin'):
            show_borders = lfEval("get(g:, 'Lf_PopupShowBorder', 1)") == '1'
            self._updateOptions(preview_pos, show_borders, self._preview_config)
            if lfEval("has('nvim')") == '1':
                if 'noautocmd' in self._preview_config:
                    del self._preview_config['noautocmd']
                lfCmd("call nvim_win_set_config(%d, %s)" % (self._preview_winid, str(self._preview_config)))
            else:
                lfCmd("call popup_setoptions(%d, %s)" % (self._preview_winid, str(self._preview_config)))

    def _useExistingWindow(self, title, source, line_num, jump_cmd):
        self.setOptionsForCursor()

        if self._orig_source != source:
            self._orig_source = source

            if lfEval("has('nvim')") == '1':
                if isinstance(source, int):
                    lfCmd("silent noautocmd call nvim_win_set_buf(%d, %d)" % (self._preview_winid, source))
                    self._setWinOptions(self._preview_winid)
                else:
                    try:
                        if self._isBinaryFile(source):
                            lfCmd("""let content = map(range(128), '"^@"')""")
                        else:
                            lfCmd("let content = readfile('%s', '', 20480)" % escQuote(source))
                    except vim.error as e:
                        lfPrintError(e)
                        return
                    if lfEval("!exists('g:Lf_preview_scratch_buffer') || !bufexists(g:Lf_preview_scratch_buffer)") == '1':
                        lfCmd("noautocmd let g:Lf_preview_scratch_buffer = nvim_create_buf(0, 1)")
                    lfCmd("noautocmd call nvim_buf_set_option(g:Lf_preview_scratch_buffer, 'undolevels', -1)")
                    lfCmd("noautocmd call nvim_buf_set_option(g:Lf_preview_scratch_buffer, 'modeline', v:true)")
                    lfCmd("noautocmd call nvim_buf_set_lines(g:Lf_preview_scratch_buffer, 0, -1, v:false, content)")
                    lfCmd("silent noautocmd call nvim_win_set_buf(%d, g:Lf_preview_scratch_buffer)" % self._preview_winid)
                    preview_filetype = lfEval("getbufvar(winbufnr(%d), '&ft')" % self._preview_winid)

                    cur_filetype = getExtension(source)
                    if cur_filetype != preview_filetype:
                        if cur_filetype is None:
                            lfCmd("call win_execute(%d, 'silent! doautocmd filetypedetect BufNewFile %s')"
                                  % (self._preview_winid, escQuote(source)))
                        else:
                            lfCmd("call win_execute(%d, 'noautocmd set ft=%s')" % (self._preview_winid, cur_filetype))
                            lfCmd("call win_execute(%d, 'set syntax=%s')" % (self._preview_winid, cur_filetype))
            elif lfEval("exists('*popup_setbuf')") == "1":
                if isinstance(source, int):
                    lfCmd("call popup_setbuf(%d, %d)" % (self._preview_winid, source))
                else:
                    filename = source
                    try:
                        if self._isBinaryFile(filename):
                            lfCmd("""let content = map(range(128), '"^@"')""")
                        else:
                            lfCmd("let content = readfile('%s', '', 20480)" % escQuote(filename))
                    except vim.error as e:
                        lfPrintError(e)
                        return
                    lfCmd("silent call popup_setbuf(%d, bufadd('/Lf_preview_%d'))" % (self._preview_winid, id(self)))
                    lfCmd("call win_execute(%d, 'setlocal modeline')" % self._preview_winid)
                    lfCmd("call win_execute(%d, 'setlocal undolevels=-1')" % self._preview_winid)
                    lfCmd("call win_execute(%d, 'setlocal noswapfile')" % self._preview_winid)
                    lfCmd("call win_execute(%d, 'setlocal nobuflisted')" % self._preview_winid)
                    lfCmd("call win_execute(%d, 'setlocal bufhidden=hide')" % self._preview_winid)
                    lfCmd("call win_execute(%d, 'setlocal buftype=nofile')" % self._preview_winid)
                    lfCmd("noautocmd call popup_settext(%d, content)" % self._preview_winid)
                    cur_filetype = lfEval("getbufvar(winbufnr(%d), '&ft')" % self._preview_winid)

                    if cur_filetype != getExtension(filename):
                        lfCmd("call win_execute(%d, 'silent! doautocmd filetypedetect BufNewFile %s')"
                              % (self._preview_winid, escQuote(filename)))
            else:
                if isinstance(source, int):
                    lfCmd("noautocmd call popup_settext(%d, getbufline(%d, 1, 20480))" % (self._preview_winid, source))
                    filename = vim.buffers[source].name
                else:
                    filename = source
                    try:
                        if self._isBinaryFile(filename):
                            lfCmd("""let content = map(range(128), '"^@"')""")
                        else:
                            lfCmd("let content = readfile('%s', '', 20480)" % escQuote(filename))
                    except vim.error as e:
                        lfPrintError(e)
                        return
                    lfCmd("noautocmd call popup_settext(%d, content)" % self._preview_winid)

                cur_filetype = getExtension(filename)
                if cur_filetype != self._preview_filetype:
                    lfCmd("call win_execute(%d, 'silent! doautocmd filetypedetect BufNewFile %s')" % (self._preview_winid, escQuote(filename)))
                    self._preview_filetype = lfEval("getbufvar(winbufnr(%d), '&ft')" % self._preview_winid)

            self._setWinOptions(self._preview_winid)

        if jump_cmd:
            lfCmd("""call win_execute(%d, '%s')""" % (self._preview_winid, escQuote(jump_cmd)))
            lfCmd("call win_execute(%d, 'norm! zz')" % self._preview_winid)
        elif line_num > 0:
            lfCmd("""call win_execute(%d, "call cursor(%d, 1)")""" % (self._preview_winid, line_num))
            lfCmd("call win_execute(%d, 'norm! zz')" % self._preview_winid)
        else:
            lfCmd("call win_execute(%d, 'norm! gg')" % self._preview_winid)

    @ignoreEvent('BufRead,BufReadPre,BufReadPost')
    def _createPopupPreview(self, title, source, line_num, jump_cmd=''):
        """
        Args:
            source:
                if the type is int, it is a buffer number
                if the type is str, it is a file name

        return False if use existing window, otherwise True
        """
        self._is_previewed = True
        line_num = int(line_num)

        if self.isPreviewWindowOpen():
            self._useExistingWindow(title, source, line_num, jump_cmd)
            return False

        if self._getInstance().getWinPos() in ('popup', 'floatwin'):
            self._createPopupModePreview(title, source, line_num, jump_cmd)
            return True

        win_pos = self._getInstance().getWinPos()
        show_borders = lfEval("get(g:, 'Lf_PopupShowBorder', 1)") == '1'
        preview_pos = self._arguments.get("--preview-position", [""])[0]
        if preview_pos == "":
            preview_pos = lfEval("get(g:, 'Lf_PreviewPosition', 'top')")

        if lfEval("has('nvim')") == '1':
            if win_pos == 'bottom':
                if preview_pos.lower() == 'topleft':
                    relative = 'editor'
                    anchor = "SW"
                    width = self._getInstance().window.width // 2
                    height = self._getInstance().window.row
                    row = self._getInstance().window.row
                    col = 0
                elif preview_pos.lower() == 'topright':
                    relative = 'editor'
                    anchor = "SW"
                    width = self._getInstance().window.width // 2
                    height = self._getInstance().window.row
                    row = self._getInstance().window.row
                    col = self._getInstance().window.width - width
                elif preview_pos.lower() == 'right':
                    relative = 'editor'
                    anchor = "NW"
                    width = self._getInstance().window.width // 2
                    height = self._getInstance().window.height
                    row = self._getInstance().window.row
                    col = self._getInstance().window.width - width
                else: # preview_pos.lower() == 'top'
                    relative = 'editor'
                    anchor = "SW"
                    width = self._getInstance().window.width
                    height = self._getInstance().window.row
                    row = self._getInstance().window.row
                    col = 0
            elif win_pos == 'top':
                if preview_pos.lower() == 'bottom':
                    relative = 'editor'
                    anchor = "NW"
                    width = self._getInstance().window.width
                    height = int(lfEval("&lines")) - self._getInstance().window.height - 2
                    row = self._getInstance().window.height
                    col = 0
                else: # preview_pos.lower() == 'right'
                    relative = 'editor'
                    anchor = "NW"
                    width = self._getInstance().window.width // 2
                    height = self._getInstance().window.height
                    row = self._getInstance().window.row
                    col = self._getInstance().window.width - width
            elif win_pos == 'left':
                relative = 'editor'
                anchor = "NW"
                width = int(lfEval("&columns")) - 1 - self._getInstance().window.width
                height = self._getInstance().window.height
                row = self._getInstance().window.row
                col = self._getInstance().window.width + 1
            elif win_pos == 'right':
                relative = 'editor'
                anchor = "NW"
                width = int(lfEval("&columns")) - 1 - self._getInstance().window.width
                height = self._getInstance().window.height
                row = self._getInstance().window.row
                col = 0
            elif win_pos == 'fullScreen':
                relative = 'editor'
                anchor = "NW"
                width = self._getInstance().window.width // 2
                height = self._getInstance().window.height
                row = self._getInstance().window.row
                col = self._getInstance().window.width - width
            else:
                relative = 'editor'
                anchor = "NW"
                width = self._getInstance().window.width // 2
                height = self._getInstance().window.height
                row = self._getInstance().window.row
                col = self._getInstance().window.col + self._getInstance().window.width - width

            config = {
                    "relative": relative,
                    "anchor"  : anchor,
                    "height"  : height,
                    "width"   : width,
                    "zindex"  : 20480,
                    "row"     : row,
                    "col"     : col,
                    "noautocmd": 1
                    }

            self._updateOptions(preview_pos, show_borders, config)
            self._createPreviewWindow(config, source, line_num, jump_cmd)
        else:
            if win_pos == 'bottom':
                if preview_pos.lower() == 'topleft':
                    maxwidth = self._getInstance().window.width // 2
                    maxheight = self._getInstance().window.row - 1
                    pos = "botleft"
                    line = self._getInstance().window.row
                    col = 1
                elif preview_pos.lower() == 'topright':
                    maxwidth = self._getInstance().window.width // 2
                    maxheight = self._getInstance().window.row - 1
                    pos = "botleft"
                    line = self._getInstance().window.row
                    col = self._getInstance().window.width - maxwidth + 1
                elif preview_pos.lower() == 'right':
                    maxwidth = self._getInstance().window.width // 2
                    maxheight = self._getInstance().window.height - 1
                    pos = "topleft"
                    line = self._getInstance().window.row + 1
                    col = self._getInstance().window.width - maxwidth + 1
                else: # preview_pos.lower() == 'top'
                    maxwidth = self._getInstance().window.width
                    maxheight = self._getInstance().window.row - 1
                    pos = "botleft"
                    line = self._getInstance().window.row
                    col = 1
            elif win_pos == 'top':
                if preview_pos.lower() == 'bottom':
                    maxwidth = self._getInstance().window.width
                    maxheight = int(lfEval("&lines")) - self._getInstance().window.height - 3
                    pos = "topleft"
                    line = self._getInstance().window.height + 1
                    col = 1
                else: # preview_pos.lower() == 'right'
                    maxwidth = self._getInstance().window.width // 2
                    maxheight = self._getInstance().window.height - 1
                    pos = "topleft"
                    line = self._getInstance().window.row + 1
                    col = self._getInstance().window.width - maxwidth + 1
            elif win_pos == 'left':
                maxwidth = int(lfEval("&columns")) - 1 - self._getInstance().window.width
                maxheight = self._getInstance().window.height - 1
                pos = "topleft"
                line = self._getInstance().window.row + 1
                col = self._getInstance().window.width + 2
            elif win_pos == 'right':
                maxwidth = int(lfEval("&columns")) - 1 - self._getInstance().window.width
                maxheight = self._getInstance().window.height - 1
                pos = "topleft"
                line = self._getInstance().window.row + 1
                col = 1
            elif win_pos == 'fullScreen':
                maxwidth = self._getInstance().window.width // 2
                maxheight = self._getInstance().window.height - 1
                pos = "topleft"
                line = self._getInstance().window.row + 1
                col = self._getInstance().window.width - maxwidth + 1
            else:
                maxwidth = self._getInstance().window.width // 2
                maxheight = self._getInstance().window.height - 1
                pos = "topleft"
                line = self._getInstance().window.row + 1
                col = self._getInstance().window.col + self._getInstance().window.width - maxwidth + 1

            options = {
                    "title":           " Preview ",
                    "maxwidth":        maxwidth,
                    "minwidth":        maxwidth,
                    "maxheight":       maxheight,
                    "minheight":       maxheight,
                    "zindex":          20480,
                    "pos":             pos,
                    "line":            line,
                    "col":             col,
                    "scrollbar":       0,
                    "padding":         [0, 0, 0, 0],
                    "borderhighlight": ["Lf_hl_previewTitle"],
                    "filter":          "leaderf#popupModePreviewFilter",
                    }

            self._updateOptions(preview_pos, show_borders, options)
            self._createPreviewWindow(options, source, line_num, jump_cmd)

        return True

    def _updateOptions(self, preview_pos, show_borders, options):
        if lfEval("has('nvim')") == '1':
            if preview_pos.lower() == 'cursor':
                options["anchor"] = "NW"
                options["width"] = self._getInstance().window.width
                row = int(lfEval("screenpos(%d, line('.'), 1)" % self._getInstance().windowId)['row'])
                height = int(lfEval("&lines")) - 2 - row

                if height * 2 < int(lfEval("&lines")) - 2:
                    height = row - 1
                    row = 0

                options["height"] = height
                options["row"] = row
                options["col"] = self._getInstance().window.col

            if show_borders:
                popup_borders = lfEval("g:Lf_PopupBorders")
                borderchars = [
                        [popup_borders[4],  "Lf_hl_popupBorder"],
                        [popup_borders[0],  "Lf_hl_popupBorder"],
                        [popup_borders[5],  "Lf_hl_popupBorder"],
                        [popup_borders[1],  "Lf_hl_popupBorder"],
                        [popup_borders[6],  "Lf_hl_popupBorder"],
                        [popup_borders[2],  "Lf_hl_popupBorder"],
                        [popup_borders[7],  "Lf_hl_popupBorder"],
                        [popup_borders[3],  "Lf_hl_popupBorder"]
                        ]
                options["border"] = borderchars
                options["height"] -= 2
                options["width"] -= 2
                if lfEval("has('nvim-0.9.0')") == '1':
                    options["title"] = " Preview "
                    options["title_pos"] = "center"
        else:
            if preview_pos.lower() == 'cursor':
                options["maxwidth"] = self._getInstance().window.width
                options["minwidth"] = self._getInstance().window.width
                row = int(lfEval("screenpos(%d, line('.'), 1)" % self._getInstance().windowId)['row'])
                maxheight = int(lfEval("&lines")) - 2 - row - 1

                if maxheight * 2 < int(lfEval("&lines")) - 3:
                    maxheight = int(lfEval("&lines")) - maxheight - 5

                options["maxheight"] = maxheight
                options["minheight"] = maxheight
                options["pos"] = "botleft"
                options["line"] = "cursor-1"
                options["col"] = self._getInstance().window.col + 1

            if show_borders:
                options["border"] = []
                options["borderchars"] = lfEval("g:Lf_PopupBorders")
                options["maxwidth"] -= 2
                options["minwidth"] -= 2
                options["maxheight"] -= 1
                options["minheight"] -= 1
                options["borderhighlight"] = ["Lf_hl_popupBorder"]

    def _needPreview(self, preview, preview_in_popup):
        """
        Args:
            preview:
                if True, always preview the result no matter what `g:Lf_PreviewResult` is.

            preview_in_popup:
                whether preview in popup, if value is true, it means
                (lfEval("get(g:, 'Lf_PreviewInPopup', 1)") == '1' or self._getInstance().getWinPos() in ('popup', 'floatwin'))
        """
        if self._inHelpLines():
            return False

        if preview:
            return True

        # non popup preview does not support automatic preview
        if not preview_in_popup:
            return False

        if "--auto-preview" in self._arguments:
            return True

        if "--no-auto-preview" in self._arguments:
            return False

        preview_dict = {k.lower(): v for k, v in lfEval("g:Lf_PreviewResult").items()}
        category = self._getExplorer().getStlCategory()
        if int(preview_dict.get(category.lower(), 1)) == 0:
            return False

        if self._getInstance().empty():
            return False

        return True

    def _getInstance(self):
        if self._instance is None:
            self._instance = LfInstance(self, self._getExplorer().getStlCategory(),
                                        self._cli,
                                        self._beforeEnter,
                                        self._afterEnter,
                                        self._beforeExit,
                                        self._afterExit)
        return self._instance

    def _createHelpHint(self):
        help = []
        if not self._show_help:
            if lfEval("get(g:, 'Lf_HideHelp', 0)") == '0':
                help.append('" Press <F1> for help')
                help.append('" ---------------------------------------------------------')
        else:
            help += self._createHelp()
        self._help_length = len(help)
        orig_row = self._getInstance().window.cursor[0]
        if self._getInstance().isReverseOrder():
            self._getInstance().buffer.options['modifiable'] = True
            self._getInstance().buffer.append(help[::-1])
            self._getInstance().buffer.options['modifiable'] = False
            buffer_len = len(self._getInstance().buffer)
            if buffer_len < self._initial_count:
                if "--nowrap" not in self._arguments:
                    self._getInstance().window.height = min(self._initial_count,
                            self._getInstance()._actualLength(self._getInstance().buffer))
                else:
                    self._getInstance().window.height = buffer_len
            elif self._getInstance().window.height < self._initial_count:
                self._getInstance().window.height = self._initial_count
            lfCmd("normal! Gzb")
            self._getInstance().window.cursor = (orig_row, 0)
        else:
            self._getInstance().buffer.options['modifiable'] = True
            self._getInstance().buffer.append(help, 0)
            self._getInstance().buffer.options['modifiable'] = False
            self._getInstance().window.cursor = (orig_row + self._help_length, 0)
            self._getInstance().mimicCursor()

        self._getInstance().refreshPopupStatusline()

    def _hideHelp(self):
        self._getInstance().buffer.options['modifiable'] = True
        if self._getInstance().isReverseOrder():
            orig_row = self._getInstance().window.cursor[0]
            countdown = len(self._getInstance().buffer) - orig_row - self._help_length
            if self._help_length > 0:
                del self._getInstance().buffer[-self._help_length:]

            self._getInstance().buffer[:] = self._getInstance().buffer[-self._initial_count:]
            lfCmd("normal! Gzb")

            if 0 < countdown < self._initial_count:
                self._getInstance().window.cursor = (len(self._getInstance().buffer) - countdown, 0)
            else:
                self._getInstance().window.cursor = (len(self._getInstance().buffer), 0)

            self._getInstance().setLineNumber()
        else:
            del self._getInstance().buffer[:self._help_length]
            if self._help_length > 0 and self._getInstance().getWinPos() == 'popup':
                lfCmd("call win_execute(%d, 'norm! %dk')" % (self._getInstance().getPopupWinId(), self._help_length))

        self._help_length = 0
        self._getInstance().refreshPopupStatusline()

    def _inHelpLines(self):
        if self._getInstance().isReverseOrder():
            if self._getInstance().window.cursor[0] > len(self._getInstance().buffer) - self._help_length:
                return True
        elif self._getInstance().window.cursor[0] <= self._help_length:
            return True
        return False

    def _getExplorer(self):
        if self._explorer is None:
            self._explorer = self._getExplClass()()
        return self._explorer

    def _resetAutochdir(self):
        if int(lfEval("&autochdir")) == 1:
            self._autochdir = 1
            lfCmd("set noautochdir")
        else:
            self._autochdir = 0

    def _setAutochdir(self):
        if self._autochdir == 1:
            # When autochdir is set, Vim will change the current working directory
            # to the directory containing the file which was opened or selected.
            lfCmd("set autochdir")

    def _toUpInPopup(self):
        if self.isPreviewWindowOpen():
            scroll_step_size = int(lfEval("get(g:, 'Lf_PreviewScrollStepSize', 1)"))
            if lfEval("has('nvim')") == '1':
                cur_winid = lfEval("win_getid()")
                lfCmd("noautocmd call win_gotoid(%d)" % self._preview_winid)
                lfCmd("norm! %dk" % (scroll_step_size))
                lfCmd("redraw")
                lfCmd("noautocmd call win_gotoid(%s)" % cur_winid)
            else:
                lfCmd("call win_execute(%d, 'norm! %dk')" % (self._preview_winid, scroll_step_size))

    def _toDownInPopup(self):
        if self.isPreviewWindowOpen():
            scroll_step_size = int(lfEval("get(g:, 'Lf_PreviewScrollStepSize', 1)"))
            if lfEval("has('nvim')") == '1':
                cur_winid = lfEval("win_getid()")
                lfCmd("noautocmd call win_gotoid(%d)" % self._preview_winid)
                lfCmd("norm! %dj" % (scroll_step_size))
                lfCmd("redraw")
                lfCmd("noautocmd call win_gotoid(%s)" % cur_winid)
            else:
                lfCmd("call win_execute(%d, 'norm! %dj')" % (self._preview_winid, scroll_step_size))

    def move(self, direction):
        """
        direction is in {'j', 'k'}
        """
        if (direction == 'j' and self._getInstance().window.cursor[0] == len(self._getInstance().buffer)
            and self._circular_scroll):
            lfCmd("noautocmd call win_execute(%d, 'norm! gg')" % (self._getInstance().getPopupWinId()))
        elif direction == 'k' and self._getInstance().window.cursor[0] == 1 and self._circular_scroll:
            lfCmd("noautocmd call win_execute(%d, 'norm! G')" % (self._getInstance().getPopupWinId()))
        else:
            lfCmd("noautocmd call win_execute(%d, 'norm! %s')" % (self._getInstance().getPopupWinId(), direction))

    def moveAndPreview(self, direction):
        """
        direction is in {'j', 'k', 'Down', 'Up', 'PageDown', 'PageUp'}
        """
        count = int(lfEval("v:count"))

        if (direction in ("j", "Down") and self._getInstance().window.cursor[0] == len(self._getInstance().buffer)
            and self._circular_scroll):
            lfCmd('noautocmd exec "norm! gg"')
        elif direction in ("k", "Up") and self._getInstance().window.cursor[0] == 1 and self._circular_scroll:
            lfCmd('noautocmd exec "norm! G"')
        else:
            if len(direction) > 1:
                lfCmd(r'noautocmd exec "norm! {}\<{}>"'.format(count, direction))
            else:
                lfCmd('noautocmd exec "norm! {}{}"'.format(count, direction))

        if self._getInstance().getWinPos() == 'floatwin':
            self._cli._buildPopupPrompt()

        self._previewResult(False)

    def _toUp(self):
        if self._getInstance().getWinPos() == 'popup':
            if self._getInstance().window.cursor[0] == 1 and self._circular_scroll:
                lfCmd("noautocmd call win_execute(%d, 'norm! G')" % (self._getInstance().getPopupWinId()))
            else:
                lfCmd("noautocmd call win_execute(%d, 'norm! k')" % (self._getInstance().getPopupWinId()))
            self._getInstance().refreshPopupStatusline()
            return

        adjust = False
        if self._getInstance().isReverseOrder() and self._getInstance().getCurrentPos()[0] == 1:
            adjust = True
            self._setResultContent()
            if self._cli.pattern and self._cli.isFuzzy \
                    and len(self._highlight_pos) < (len(self._getInstance().buffer) - self._help_length) // self._getUnit() \
                    and len(self._highlight_pos) < int(lfEval("g:Lf_NumberOfHighlight")):
                self._highlight_method()

        if self._getInstance().window.cursor[0] == 1 and self._circular_scroll:
            lfCmd("noautocmd norm! G")
        else:
            lfCmd("noautocmd norm! k")

        if adjust:
            lfCmd("norm! zt")

        self._getInstance().setLineNumber()
        lfCmd("setlocal cursorline!")   # these two help to redraw the statusline,
        lfCmd("setlocal cursorline!")   # also fix a weird bug of vim

    def _toDown(self):
        if self._getInstance().getWinPos() == 'popup':
            if self._getInstance().window.cursor[0] == len(self._getInstance().buffer) and self._circular_scroll:
                lfCmd("noautocmd call win_execute(%d, 'norm! gg')" % (self._getInstance().getPopupWinId()))
            else:
                lfCmd("noautocmd call win_execute(%d, 'norm! j')" % (self._getInstance().getPopupWinId()))
            self._getInstance().refreshPopupStatusline()
            return

        if not self._getInstance().isReverseOrder() \
                and self._getInstance().getCurrentPos()[0] == self._initial_count:
            self._setResultContent()

        if self._getInstance().window.cursor[0] == len(self._getInstance().buffer) and self._circular_scroll:
            lfCmd("noautocmd norm! gg")
        else:
            lfCmd("noautocmd norm! j")
        self._getInstance().setLineNumber()
        lfCmd("setlocal cursorline!")   # these two help to redraw the statusline,
        lfCmd("setlocal cursorline!")   # also fix a weird bug of vim

    def _pageUp(self):
        if self._getInstance().getWinPos() == 'popup':
            lfCmd(r"""call win_execute(%d, 'exec "norm! \<PageUp>"')""" % (self._getInstance().getPopupWinId()))
            self._getInstance().refreshPopupStatusline()
            return

        if self._getInstance().isReverseOrder():
            self._setResultContent()
            if self._cli.pattern and self._cli.isFuzzy \
                    and len(self._highlight_pos) < (len(self._getInstance().buffer) - self._help_length) // self._getUnit() \
                    and len(self._highlight_pos) < int(lfEval("g:Lf_NumberOfHighlight")):
                self._highlight_method()

        lfCmd(r'noautocmd exec "norm! \<PageUp>"')

        self._getInstance().setLineNumber()

    def _pageDown(self):
        if self._getInstance().getWinPos() == 'popup':
            lfCmd(r"""call win_execute(%d, 'exec "norm! \<PageDown>"')""" % (self._getInstance().getPopupWinId()))
            self._getInstance().refreshPopupStatusline()
            return

        if not self._getInstance().isReverseOrder():
            self._setResultContent()

        lfCmd(r'noautocmd exec "norm! \<PageDown>"')

        self._getInstance().setLineNumber()

    def _leftClick(self):
        if self._getInstance().getWinPos() == 'popup':
            if int(lfEval("has('patch-8.1.2292')")) == 1:
                lfCmd("""call win_execute(v:mouse_winid, "exec v:mouse_lnum")""")
                lfCmd("""call win_execute(v:mouse_winid, "exec 'norm!'.v:mouse_col.'|'")""")
            exit_loop = False
        elif self._getInstance().window.number == int(lfEval("v:mouse_win")):
            lfCmd("noautocmd silent! call win_gotoid(v:mouse_winid)")
            lfCmd("exec v:mouse_lnum")
            lfCmd("exec 'norm!'.v:mouse_col.'|'")
            self._getInstance().setLineNumber()
            self.clearSelections()
            exit_loop = False
        elif self._preview_winid == int(lfEval("v:mouse_winid")):
            if lfEval("has('nvim')") == '1':
                lfCmd("noautocmd silent! call win_gotoid(v:mouse_winid)")
                lfCmd("exec v:mouse_lnum")
                lfCmd("exec 'norm!'.v:mouse_col.'|'")

            exit_loop = False
        else:
            self.quit()
            exit_loop = True
        return exit_loop

    def _scrollUp(self):
        if lfEval('exists("*getmousepos")') == '1':
            lfCmd(r"""call win_execute(getmousepos().winid, "norm! 3\<C-Y>")""")
        else:
            self._toUp()

    def _scrollDown(self):
        if lfEval('exists("*getmousepos")') == '1':
            lfCmd(r"""call win_execute(getmousepos().winid, "norm! 3\<C-E>")""")
        else:
            self._toDown()

    def _quickSelect(self):
        selection = int(self._cli.last_char)
        if selection == 0:
            selection = 10

        line_num = self._help_length + selection
        if line_num > len(self._getInstance().buffer):
            return False

        if self._getInstance().isReverseOrder():
            pass
        else:
            if self._getInstance().getWinPos() == 'popup':
                lfCmd("""call win_execute(%d, "exec %d")""" % (self._getInstance().getPopupWinId(), line_num))
            else:
                lfCmd("exec %d" % line_num)

            action = lfEval("get(g:, 'Lf_QuickSelectAction', 'c')")
            if action != '' and action in "hvtc":
                if action == 'c':
                    action = ''
                self.accept(action)
                return True
            else:
                self._previewResult(False)
                return False

    def _search(self, content, is_continue=False, step=0):
        if not is_continue:
            self.clearSelections()
            self._clearHighlights()
            self._clearHighlightsPos()
            self._cli.highlightMatches()

        if not self._cli.pattern:   # e.g., when <BS> or <Del> is typed
            if self._empty_query and self._getExplorer().getStlCategory() in ["File"]:
                self._guessSearch(self._content)
            else:
                self._getInstance().setBuffer(content[:self._initial_count])
                self._getInstance().setStlResultsCount(len(content), True)
                self._result_content = []
            self._previewResult(False)
            return

        if self._cli.isFuzzy:
            self._fuzzySearch(content, is_continue, step)
        else:
            self._regexSearch(content, is_continue, step)

        self._previewResult(False)

    def _filter(self, step, filter_method, content, is_continue,
                use_fuzzy_engine=False, return_index=False):
        """ Construct a list from result of filter_method(content).

        Args:
            step: An integer to indicate the number of lines to filter one time.
            filter_method: A function to apply `content` as parameter and
                return an iterable.
            content: The list to be filtered.
        """
        unit = self._getUnit()
        step = step // unit * unit
        length = len(content)
        if self._index == 0:
            self._cb_content = []
            self._result_content = []
            self._index = min(step, length)
            cur_content = content[:self._index]
        else:
            if not is_continue and self._result_content:
                if self._cb_content:
                    self._cb_content += self._result_content
                else:
                    self._cb_content = self._result_content

            if len(self._cb_content) >= step:
                cur_content = self._cb_content[:step]
                self._cb_content = self._cb_content[step:]
            else:
                cur_content = self._cb_content
                left = step - len(self._cb_content)
                self._cb_content = []
                if self._index < length:
                    end = min(self._index + left, length)
                    cur_content += content[self._index:end]
                    self._index = end

        if self._cli.isAndMode:
            result, highlight_methods = filter_method(cur_content)
            if is_continue:
                self._previous_result = (self._previous_result[0] + result[0],
                                         self._previous_result[1] + result[1])
                result = self._previous_result
            else:
                self._previous_result = result
            return (result, highlight_methods)
        elif use_fuzzy_engine:
            if return_index:
                mode = 0 if self._cli.isFullPath else 1
                tmp_content = [self._getDigest(line, mode) for line in cur_content]
                result = filter_method(source=tmp_content)
                result = (result[0], [cur_content[i] for i in result[1]])
            else:
                result = filter_method(source=cur_content)

            if is_continue:
                result = fuzzyEngine.merge(self._previous_result, result)

            self._previous_result = result
        else:
            result = list(filter_method(cur_content))
            if is_continue:
                self._previous_result += result
                result = self._previous_result
            else:
                self._previous_result = result

        return result

    def _fuzzyFilter(self, is_full_path, get_weight, iterable):
        """
        return a list, each item is a pair (weight, line)
        """
        getDigest = partial(self._getDigest, mode=0 if is_full_path else 1)
        pairs = ((get_weight(getDigest(line)), line) for line in iterable)
        MIN_WEIGHT = fuzzyMatchC.MIN_WEIGHT if is_fuzzyMatch_C else FuzzyMatch.MIN_WEIGHT
        return (p for p in pairs if p[0] > MIN_WEIGHT)

    def _fuzzyFilterEx(self, is_full_path, get_weight, iterable):
        """
        return a tuple, (weights, indices)
        """
        getDigest = partial(self._getDigest, mode=0 if is_full_path else 1)
        if self._getUnit() > 1: # currently, only BufTag's _getUnit() is 2
            iterable = itertools.islice(iterable, 0, None, self._getUnit())
        pairs = ((get_weight(getDigest(line)), i) for i, line in enumerate(iterable))
        MIN_WEIGHT = fuzzyMatchC.MIN_WEIGHT if is_fuzzyMatch_C else FuzzyMatch.MIN_WEIGHT
        result = [p for p in pairs if p[0] > MIN_WEIGHT]
        if len(result) == 0:
            weights, indices = [], []
        else:
            weights, indices = zip(*result)
        return (list(weights), list(indices))

    def _refineFilter(self, first_get_weight, get_weight, iterable):
        getDigest = self._getDigest
        triples = ((first_get_weight(getDigest(line, 1)),
                    get_weight(getDigest(line, 2)), line)
                    for line in iterable)
        MIN_WEIGHT = fuzzyMatchC.MIN_WEIGHT if is_fuzzyMatch_C else FuzzyMatch.MIN_WEIGHT
        return ((i[0] + i[1], i[2]) for i in triples if i[0] > MIN_WEIGHT and i[1] > MIN_WEIGHT)

    def _andModeFilter(self, iterable):
        encoding = lfEval("&encoding")
        cur_content = iterable
        weight_lists = []
        highlight_methods = []
        for p in self._cli.pattern:
            use_fuzzy_engine = False
            if self._fuzzy_engine and isAscii(p) and self._getUnit() == 1: # currently, only BufTag's _getUnit() is 2
                use_fuzzy_engine = True
                pattern = fuzzyEngine.initPattern(p)
                if self._getExplorer().getStlCategory() == "File" and self._cli.isFullPath:
                    filter_method = partial(fuzzyEngine.fuzzyMatchEx, engine=self._fuzzy_engine, pattern=pattern,
                                            is_name_only=False, sort_results=False, is_and_mode=True)
                elif self._getExplorer().getStlCategory() in ["Self", "Buffer", "Mru", "BufTag",
                        "Function", "History", "Cmd_History", "Search_History", "Tag", "Rg", "Filetype",
                        "Command", "Window", "QuickFix", "LocList"]:
                    filter_method = partial(fuzzyEngine.fuzzyMatchEx, engine=self._fuzzy_engine, pattern=pattern,
                                            is_name_only=True, sort_results=False, is_and_mode=True)
                else:
                    filter_method = partial(fuzzyEngine.fuzzyMatchEx, engine=self._fuzzy_engine, pattern=pattern,
                                            is_name_only=not self._cli.isFullPath, sort_results=False, is_and_mode=True)

                getHighlights = partial(fuzzyEngine.getHighlights, engine=self._fuzzy_engine,
                                        pattern=pattern, is_name_only=not self._cli.isFullPath)
                highlight_method = partial(self._highlight, self._cli.isFullPath, getHighlights, True, clear=False)
            elif is_fuzzyMatch_C and isAscii(p):
                pattern = fuzzyMatchC.initPattern(p)
                if self._getExplorer().getStlCategory() == "File" and self._cli.isFullPath:
                    getWeight = partial(fuzzyMatchC.getWeight, pattern=pattern, is_name_only=False)
                    getHighlights = partial(fuzzyMatchC.getHighlights, pattern=pattern, is_name_only=False)
                else:
                    getWeight = partial(fuzzyMatchC.getWeight, pattern=pattern, is_name_only=True)
                    getHighlights = partial(fuzzyMatchC.getHighlights, pattern=pattern, is_name_only=True)

                filter_method = partial(self._fuzzyFilterEx, self._cli.isFullPath, getWeight)
                highlight_method = partial(self._highlight, self._cli.isFullPath, getHighlights, clear=False)
            else:
                fuzzy_match = FuzzyMatch(p, encoding)
                if self._getExplorer().getStlCategory() == "File" and self._cli.isFullPath:
                    filter_method = partial(self._fuzzyFilterEx,
                                            self._cli.isFullPath,
                                            fuzzy_match.getWeight2)
                elif self._getExplorer().getStlCategory() in ["Self", "Buffer", "Mru", "BufTag",
                        "Function", "History", "Cmd_History", "Search_History", "Tag", "Rg", "Filetype",
                        "Command", "Window", "QuickFix", "LocList"]:
                    filter_method = partial(self._fuzzyFilterEx,
                                            self._cli.isFullPath,
                                            fuzzy_match.getWeight3)
                else:
                    filter_method = partial(self._fuzzyFilterEx,
                                            self._cli.isFullPath,
                                            fuzzy_match.getWeight)

                highlight_method = partial(self._highlight,
                                           self._cli.isFullPath,
                                           fuzzy_match.getHighlights,
                                           clear=False)

            if use_fuzzy_engine:
                mode = 0 if self._cli.isFullPath else 1
                tmp_content = [self._getDigest(line, mode) for line in cur_content]
                result = filter_method(source=tmp_content)
            else:
                result = filter_method(cur_content)

            for i, wl in enumerate(weight_lists):
                weight_lists[i] = [wl[j] for j in result[1]]

            weight_lists.append(result[0])
            if self._getUnit() > 1: # currently, only BufTag's _getUnit() is 2
                unit = self._getUnit()
                result_content = [cur_content[i*unit:i*unit + unit] for i in result[1]]
                cur_content = list(itertools.chain.from_iterable(result_content))
            else:
                cur_content = [cur_content[i] for i in result[1]]
                result_content = cur_content

            highlight_methods.append(highlight_method)

        weights = [sum(i) for i in zip(*weight_lists)]

        return ((weights, result_content), highlight_methods)

    def _fuzzySearch(self, content, is_continue, step):
        encoding = lfEval("&encoding")
        use_fuzzy_engine = False
        use_fuzzy_match_c = False
        do_sort = "--no-sort" not in self._arguments
        if self._cli.isAndMode:
            filter_method = self._andModeFilter
        elif self._cli.isRefinement:
            if self._cli.pattern[1] == '':      # e.g. abc;
                if self._fuzzy_engine and isAscii(self._cli.pattern[0]):
                    use_fuzzy_engine = True
                    return_index = True
                    pattern = fuzzyEngine.initPattern(self._cli.pattern[0])
                    filter_method = partial(fuzzyEngine.fuzzyMatchEx, engine=self._fuzzy_engine,
                                            pattern=pattern, is_name_only=True, sort_results=do_sort)
                    getHighlights = partial(fuzzyEngine.getHighlights, engine=self._fuzzy_engine,
                                            pattern=pattern, is_name_only=True)
                    highlight_method = partial(self._highlight, False, getHighlights, True)
                elif is_fuzzyMatch_C and isAscii(self._cli.pattern[0]):
                    use_fuzzy_match_c = True
                    pattern = fuzzyMatchC.initPattern(self._cli.pattern[0])
                    getWeight = partial(fuzzyMatchC.getWeight, pattern=pattern, is_name_only=True)
                    getHighlights = partial(fuzzyMatchC.getHighlights, pattern=pattern, is_name_only=True)
                    filter_method = partial(self._fuzzyFilter, False, getWeight)
                    highlight_method = partial(self._highlight, False, getHighlights)
                else:
                    fuzzy_match = FuzzyMatch(self._cli.pattern[0], encoding)
                    if "--no-sort" in self._arguments:
                        getWeight = fuzzy_match.getWeightNoSort
                    else:
                        getWeight = fuzzy_match.getWeight
                    getHighlights = fuzzy_match.getHighlights
                    filter_method = partial(self._fuzzyFilter, False, getWeight)
                    highlight_method = partial(self._highlight, False, getHighlights)
            elif self._cli.pattern[0] == '':    # e.g. ;abc
                if self._fuzzy_engine and isAscii(self._cli.pattern[1]):
                    use_fuzzy_engine = True
                    return_index = True
                    pattern = fuzzyEngine.initPattern(self._cli.pattern[1])
                    filter_method = partial(fuzzyEngine.fuzzyMatchEx, engine=self._fuzzy_engine,
                                            pattern=pattern, is_name_only=False, sort_results=do_sort)
                    getHighlights = partial(fuzzyEngine.getHighlights, engine=self._fuzzy_engine,
                                            pattern=pattern, is_name_only=False)
                    highlight_method = partial(self._highlight, True, getHighlights, True)
                elif is_fuzzyMatch_C and isAscii(self._cli.pattern[1]):
                    use_fuzzy_match_c = True
                    pattern = fuzzyMatchC.initPattern(self._cli.pattern[1])
                    getWeight = partial(fuzzyMatchC.getWeight, pattern=pattern, is_name_only=False)
                    getHighlights = partial(fuzzyMatchC.getHighlights, pattern=pattern, is_name_only=False)
                    filter_method = partial(self._fuzzyFilter, True, getWeight)
                    highlight_method = partial(self._highlight, True, getHighlights)
                else:
                    fuzzy_match = FuzzyMatch(self._cli.pattern[1], encoding)
                    if "--no-sort" in self._arguments:
                        getWeight = fuzzy_match.getWeightNoSort
                    else:
                        getWeight = fuzzy_match.getWeight
                    getHighlights = fuzzy_match.getHighlights
                    filter_method = partial(self._fuzzyFilter, True, getWeight)
                    highlight_method = partial(self._highlight, True, getHighlights)
            else:   # e.g. abc;def
                if is_fuzzyMatch_C and isAscii(self._cli.pattern[0]):
                    is_ascii_0 = True
                    pattern_0 = fuzzyMatchC.initPattern(self._cli.pattern[0])
                    getWeight_0 = partial(fuzzyMatchC.getWeight, pattern=pattern_0, is_name_only=True)
                    getHighlights_0 = partial(fuzzyMatchC.getHighlights, pattern=pattern_0, is_name_only=True)
                else:
                    is_ascii_0 = False
                    fuzzy_match_0 = FuzzyMatch(self._cli.pattern[0], encoding)
                    if "--no-sort" in self._arguments:
                        getWeight_0 = fuzzy_match_0.getWeightNoSort
                    else:
                        getWeight_0 = fuzzy_match_0.getWeight
                    getHighlights_0 = fuzzy_match_0.getHighlights

                if is_fuzzyMatch_C and isAscii(self._cli.pattern[1]):
                    is_ascii_1 = True
                    pattern_1 = fuzzyMatchC.initPattern(self._cli.pattern[1])
                    getWeight_1 = partial(fuzzyMatchC.getWeight, pattern=pattern_1, is_name_only=False)
                    getHighlights_1 = partial(fuzzyMatchC.getHighlights, pattern=pattern_1, is_name_only=False)
                else:
                    is_ascii_1 = False
                    fuzzy_match_1 = FuzzyMatch(self._cli.pattern[1], encoding)
                    if "--no-sort" in self._arguments:
                        getWeight_1 = fuzzy_match_1.getWeightNoSort
                    else:
                        getWeight_1 = fuzzy_match_1.getWeight
                    getHighlights_1 = fuzzy_match_1.getHighlights

                    use_fuzzy_match_c = is_ascii_0 and is_ascii_1

                filter_method = partial(self._refineFilter, getWeight_0, getWeight_1)
                highlight_method = partial(self._highlightRefine, getHighlights_0, getHighlights_1)
        else:
            if self._fuzzy_engine and isAscii(self._cli.pattern) and self._getUnit() == 1: # currently, only BufTag's _getUnit() is 2
                use_fuzzy_engine = True
                pattern = fuzzyEngine.initPattern(self._cli.pattern)
                if self._getExplorer().getStlCategory() == "File":
                    return_index = False
                    if self._cli.isFullPath:
                        filter_method = partial(fuzzyEngine.fuzzyMatch, engine=self._fuzzy_engine, pattern=pattern,
                                                is_name_only=False, sort_results=do_sort)
                    else:
                        filter_method = partial(fuzzyEngine.fuzzyMatchPart, engine=self._fuzzy_engine,
                                                pattern=pattern, category=fuzzyEngine.Category_File,
                                                param=fuzzyEngine.createParameter(1),
                                                is_name_only=True, sort_results=do_sort)
                elif self._getExplorer().getStlCategory() == "Rg":
                    return_index = False
                    if self._cli.isFullPath or "--match-path" in self._arguments:
                        filter_method = partial(fuzzyEngine.fuzzyMatch, engine=self._fuzzy_engine, pattern=pattern,
                                                is_name_only=True, sort_results=do_sort)
                    else:
                        filter_method = partial(fuzzyEngine.fuzzyMatchPart, engine=self._fuzzy_engine,
                                                pattern=pattern, category=fuzzyEngine.Category_Rg,
                                                param=fuzzyEngine.createRgParameter(self._getExplorer().displayMulti(),
                                                    self._getExplorer().getContextSeparator(), self._has_column),
                                                is_name_only=True, sort_results=do_sort)
                elif self._getExplorer().getStlCategory() == "Tag":
                    return_index = False
                    mode = 0 if self._cli.isFullPath else 1
                    filter_method = partial(fuzzyEngine.fuzzyMatchPart, engine=self._fuzzy_engine,
                                            pattern=pattern, category=fuzzyEngine.Category_Tag,
                                            param=fuzzyEngine.createParameter(mode), is_name_only=True, sort_results=do_sort)
                elif self._getExplorer().getStlCategory() == "Gtags":
                    return_index = False
                    result_format = 1
                    if self._getExplorer().getResultFormat() in [None, "ctags-mod"]:
                        result_format = 0
                    elif self._getExplorer().getResultFormat() == "ctags-x":
                        result_format = 2
                    filter_method = partial(fuzzyEngine.fuzzyMatchPart, engine=self._fuzzy_engine,
                                            pattern=pattern, category=fuzzyEngine.Category_Gtags,
                                            param=fuzzyEngine.createGtagsParameter(0, result_format, self._match_path),
                                            is_name_only=True, sort_results=do_sort)
                elif self._getExplorer().getStlCategory() == "Line":
                    return_index = False
                    filter_method = partial(fuzzyEngine.fuzzyMatchPart, engine=self._fuzzy_engine,
                                            pattern=pattern, category=fuzzyEngine.Category_Line,
                                            param=fuzzyEngine.createParameter(1), is_name_only=True, sort_results=do_sort)
                elif self._getExplorer().getStlCategory() == "Git_diff":
                    return_index = False
                    mode = 0 if self._cli.isFullPath else 1
                    filter_method = partial(fuzzyEngine.fuzzyMatchPart, engine=self._fuzzy_engine,
                                            pattern=pattern, category=fuzzyEngine.Category_GitDiff,
                                            param=fuzzyEngine.createParameter(mode), is_name_only=False, sort_results=do_sort)
                elif self._getExplorer().getStlCategory() in ["Self", "Buffer", "Mru", "BufTag",
                        "Function", "History", "Cmd_History", "Search_History", "Filetype",
                        "Command", "Window", "QuickFix", "LocList"]:
                    return_index = True
                    filter_method = partial(fuzzyEngine.fuzzyMatchEx, engine=self._fuzzy_engine, pattern=pattern,
                                            is_name_only=True, sort_results=do_sort)
                else:
                    return_index = True
                    filter_method = partial(fuzzyEngine.fuzzyMatchEx, engine=self._fuzzy_engine, pattern=pattern,
                                            is_name_only=not self._cli.isFullPath, sort_results=do_sort)

                getHighlights = partial(fuzzyEngine.getHighlights, engine=self._fuzzy_engine,
                                        pattern=pattern, is_name_only=not self._cli.isFullPath)
                highlight_method = partial(self._highlight, self._cli.isFullPath, getHighlights, True)
            elif is_fuzzyMatch_C and isAscii(self._cli.pattern):
                use_fuzzy_match_c = True
                pattern = fuzzyMatchC.initPattern(self._cli.pattern)
                if self._getExplorer().getStlCategory() == "File" and self._cli.isFullPath:
                    getWeight = partial(fuzzyMatchC.getWeight, pattern=pattern, is_name_only=False)
                    getHighlights = partial(fuzzyMatchC.getHighlights, pattern=pattern, is_name_only=False)
                else:
                    getWeight = partial(fuzzyMatchC.getWeight, pattern=pattern, is_name_only=True)
                    getHighlights = partial(fuzzyMatchC.getHighlights, pattern=pattern, is_name_only=True)

                filter_method = partial(self._fuzzyFilter, self._cli.isFullPath, getWeight)
                highlight_method = partial(self._highlight, self._cli.isFullPath, getHighlights)
            else:
                fuzzy_match = FuzzyMatch(self._cli.pattern, encoding)
                if "--no-sort" in self._arguments:
                    filter_method = partial(self._fuzzyFilter,
                                            self._cli.isFullPath,
                                            fuzzy_match.getWeightNoSort)
                elif self._getExplorer().getStlCategory() == "File" and self._cli.isFullPath:
                    filter_method = partial(self._fuzzyFilter,
                                            self._cli.isFullPath,
                                            fuzzy_match.getWeight2)
                elif self._getExplorer().getStlCategory() in ["Self", "Buffer", "Mru", "BufTag",
                        "Function", "History", "Cmd_History", "Search_History", "Rg", "Filetype",
                        "Command", "Window", "QuickFix", "LocList"]:
                    filter_method = partial(self._fuzzyFilter,
                                            self._cli.isFullPath,
                                            fuzzy_match.getWeight3)
                else:
                    filter_method = partial(self._fuzzyFilter,
                                            self._cli.isFullPath,
                                            fuzzy_match.getWeight)

                highlight_method = partial(self._highlight,
                                           self._cli.isFullPath,
                                           fuzzy_match.getHighlights)

        if self._cli.isAndMode:
            if self._fuzzy_engine and isAscii(''.join(self._cli.pattern)):
                step = 20000 * cpu_count
            else:
                step = 10000
            pair, highlight_methods = self._filter(step, filter_method, content, is_continue)

            if do_sort:
                pairs = sorted(zip(*pair), key=operator.itemgetter(0), reverse=True)
                self._result_content = self._getList(pairs)
            else:
                self._result_content = pair[1]
        elif use_fuzzy_engine:
            if step == 0:
                if return_index == True:
                    step = 30000 * cpu_count
                else:
                    step = 50000 * cpu_count

            _, self._result_content = self._filter(step, filter_method, content, is_continue, True, return_index)
        else:
            if step == 0:
                if use_fuzzy_match_c:
                    step = 60000
                elif self._getExplorer().supportsNameOnly() and self._cli.isFullPath:
                    step = 6000
                else:
                    step = 12000

            pairs = self._filter(step, filter_method, content, is_continue)
            if "--no-sort" not in self._arguments:
                pairs.sort(key=operator.itemgetter(0), reverse=True)
            self._result_content = self._getList(pairs)

        self._getInstance().setBuffer(self._result_content[:self._initial_count])
        self._getInstance().setStlResultsCount(len(self._result_content), True)

        if self._cli.isAndMode:
            self._highlight_method = partial(self._highlight_and_mode, highlight_methods)
            self._highlight_method()
        else:
            self._highlight_method = highlight_method
            self._highlight_method()

    def _guessFilter(self, filename, suffix, dirname, icon, iterable):
        """
        return a list, each item is a pair (weight, line)
        """
        icon_len = len(icon)
        return ((FuzzyMatch.getPathWeight(filename, suffix, dirname, line[icon_len:]), line) for line in iterable)

    def _guessSearch(self, content, is_continue=False, step=0):
        if self._cur_buffer.name == '' or self._cur_buffer.options["buftype"] not in [b'', '']:
            self._getInstance().setBuffer(content[:self._initial_count])
            self._getInstance().setStlResultsCount(len(content), True)
            self._result_content = []
            return

        buffer_name = os.path.normpath(lfDecode(self._cur_buffer.name))
        if lfEval("g:Lf_ShowRelativePath") == '1':
            try:
                buffer_name = os.path.relpath(buffer_name)
            except ValueError:
                pass

        buffer_name = lfEncode(buffer_name)
        dirname, basename = os.path.split(buffer_name)
        filename, suffix = os.path.splitext(basename)
        if lfEval("get(g:, 'Lf_ShowDevIcons', 1)") == "1":
            icon = webDevIconsGetFileTypeSymbol(basename)
        else:
            icon = ''
        if self._fuzzy_engine:
            filter_method = partial(fuzzyEngine.guessMatch, engine=self._fuzzy_engine, filename=filename,
                                    suffix=suffix, dirname=dirname, icon=icon, sort_results=True)
            step = len(content)

            _, self._result_content = self._filter(step, filter_method, content, is_continue, True)
        else:
            step = len(content)
            filter_method = partial(self._guessFilter, filename, suffix, dirname, icon)
            pairs = self._filter(step, filter_method, content, is_continue)
            pairs.sort(key=operator.itemgetter(0), reverse=True)
            self._result_content = self._getList(pairs)

        self._getInstance().setBuffer(self._result_content[:self._initial_count])
        self._getInstance().setStlResultsCount(len(self._result_content), True)

    def _highlight_and_mode(self, highlight_methods):
        self._clearHighlights()
        for i, highlight_method in enumerate(highlight_methods):
            highlight_method(hl_group='Lf_hl_match' + str(i % 5))

    def _clearHighlights(self):
        if self._getInstance().getWinPos() == 'popup':
            for i in self._highlight_ids:
                lfCmd("silent! call matchdelete(%d, %d)" % (i, self._getInstance().getPopupWinId()))
        else:
            for i in self._highlight_ids:
                lfCmd("silent! call matchdelete(%d)" % i)
        self._highlight_ids = []

    def _clearHighlightsPos(self):
        self._highlight_pos = []
        self._highlight_pos_list = []
        self._highlight_refine_pos = []

    @windo
    def _resetHighlights(self):
        self._clearHighlights()

        unit = self._getUnit()
        bottom = len(self._getInstance().buffer) - self._help_length
        if self._cli.isAndMode:
            highlight_pos_list = self._highlight_pos_list
        else:
            highlight_pos_list = [self._highlight_pos]

        for n, highlight_pos in enumerate(highlight_pos_list):
            hl_group = 'Lf_hl_match' + str(n % 5)
            for i, pos in enumerate(highlight_pos):
                if self._getInstance().isReverseOrder():
                    pos = [[bottom - unit*i] + p for p in pos]
                else:
                    pos = [[unit*i + 1 + self._help_length] + p for p in pos]
                # The maximum number of positions is 8 in matchaddpos().
                for j in range(0, len(pos), 8):
                    if self._getInstance().getWinPos() == 'popup':
                        lfCmd("""call win_execute(%d, "let matchid = matchaddpos('%s', %s)")"""
                                % (self._getInstance().getPopupWinId(), hl_group, str(pos[j:j+8])))
                        id = int(lfEval("matchid"))
                    else:
                        id = int(lfEval("matchaddpos('%s', %s)" % (hl_group, str(pos[j:j+8]))))
                    self._highlight_ids.append(id)

        for i, pos in enumerate(self._highlight_refine_pos):
            if self._getInstance().isReverseOrder():
                pos = [[bottom - unit*i] + p for p in pos]
            else:
                pos = [[unit*i + 1 + self._help_length] + p for p in pos]
            # The maximum number of positions is 8 in matchaddpos().
            for j in range(0, len(pos), 8):
                if self._getInstance().getWinPos() == 'popup':
                    lfCmd("""call win_execute(%d, "let matchid = matchaddpos('Lf_hl_matchRefine', %s)")"""
                            % (self._getInstance().getPopupWinId(), str(pos[j:j+8])))
                    id = int(lfEval("matchid"))
                else:
                    id = int(lfEval("matchaddpos('Lf_hl_matchRefine', %s)" % str(pos[j:j+8])))
                self._highlight_ids.append(id)

    def _highlight(self, is_full_path, get_highlights, use_fuzzy_engine=False, clear=True, hl_group='Lf_hl_match'):
        # matchaddpos() is introduced by Patch 7.4.330
        if (lfEval("exists('*matchaddpos')") == '0' or
                lfEval("g:Lf_HighlightIndividual") == '0'):
            return
        cb = self._getInstance().buffer
        if self._getInstance().empty(): # buffer is empty.
            return

        highlight_number = int(lfEval("g:Lf_NumberOfHighlight"))
        if clear:
            self._clearHighlights()

        getDigest = partial(self._getDigest, mode=0 if is_full_path else 1)
        unit = self._getUnit()

        if self._getInstance().isReverseOrder():
            if self._help_length > 0:
                content = cb[:-self._help_length][::-1]
            else:
                content = cb[:][::-1]
        else:
            content = cb[self._help_length:]

        if use_fuzzy_engine:
            self._highlight_pos = get_highlights(source=[getDigest(line)
                                                         for line in content[:highlight_number:unit]])
        else:
            # e.g., self._highlight_pos = [ [ [2,3], [6,2] ], [ [1,4], [7,6], ... ], ... ]
            # where [2, 3] indicates the highlight starts at the 2nd column with the
            # length of 3 in bytes
            self._highlight_pos = [get_highlights(getDigest(line))
                                   for line in content[:highlight_number:unit]]
        if self._cli.isAndMode:
            self._highlight_pos_list.append(self._highlight_pos)

        bottom = len(content)
        for i, pos in enumerate(self._highlight_pos):
            start_pos = self._getDigestStartPos(content[unit*i], 0 if is_full_path else 1)
            if start_pos > 0:
                for j in range(len(pos)):
                    pos[j][0] += start_pos
            if self._getInstance().isReverseOrder():
                pos = [[bottom - unit*i] + p for p in pos]
            else:
                pos = [[unit*i + 1 + self._help_length] + p for p in pos]
            # The maximum number of positions is 8 in matchaddpos().
            for j in range(0, len(pos), 8):
                if self._getInstance().getWinPos() == 'popup':
                    lfCmd("""call win_execute(%d, "let matchid = matchaddpos('%s', %s)")"""
                            % (self._getInstance().getPopupWinId(), hl_group, str(pos[j:j+8])))
                    id = int(lfEval("matchid"))
                else:
                    id = int(lfEval("matchaddpos('%s', %s)" % (hl_group, str(pos[j:j+8]))))
                self._highlight_ids.append(id)

    def _highlightRefine(self, first_get_highlights, get_highlights):
        # matchaddpos() is introduced by Patch 7.4.330
        if (lfEval("exists('*matchaddpos')") == '0' or
                lfEval("g:Lf_HighlightIndividual") == '0'):
            return
        cb = self._getInstance().buffer
        if self._getInstance().empty(): # buffer is empty.
            return

        highlight_number = int(lfEval("g:Lf_NumberOfHighlight"))
        self._clearHighlights()

        getDigest = self._getDigest
        unit = self._getUnit()

        if self._getInstance().isReverseOrder():
            if self._help_length > 0:
                content = cb[:-self._help_length][::-1]
            else:
                content = cb[:][::-1]
        else:
            content = cb[self._help_length:]

        bottom = len(content)

        self._highlight_pos = [first_get_highlights(getDigest(line, 1))
                               for line in content[:highlight_number:unit]]
        for i, pos in enumerate(self._highlight_pos):
            start_pos = self._getDigestStartPos(content[unit*i], 1)
            if start_pos > 0:
                for j in range(len(pos)):
                    pos[j][0] += start_pos
            if self._getInstance().isReverseOrder():
                pos = [[bottom - unit*i] + p for p in pos]
            else:
                pos = [[unit*i + 1 + self._help_length] + p for p in pos]
            # The maximum number of positions is 8 in matchaddpos().
            for j in range(0, len(pos), 8):
                if self._getInstance().getWinPos() == 'popup':
                    lfCmd("""call win_execute(%d, "let matchid = matchaddpos('Lf_hl_match', %s)")"""
                            % (self._getInstance().getPopupWinId(), str(pos[j:j+8])))
                    id = int(lfEval("matchid"))
                else:
                    id = int(lfEval("matchaddpos('Lf_hl_match', %s)" % str(pos[j:j+8])))
                self._highlight_ids.append(id)

        self._highlight_refine_pos = [get_highlights(getDigest(line, 2))
                                      for line in content[:highlight_number:unit]]
        for i, pos in enumerate(self._highlight_refine_pos):
            start_pos = self._getDigestStartPos(content[unit*i], 2)
            if start_pos > 0:
                for j in range(len(pos)):
                    pos[j][0] += start_pos
            if self._getInstance().isReverseOrder():
                pos = [[bottom - unit*i] + p for p in pos]
            else:
                pos = [[unit*i + 1 + self._help_length] + p for p in pos]
            # The maximum number of positions is 8 in matchaddpos().
            for j in range(0, len(pos), 8):
                if self._getInstance().getWinPos() == 'popup':
                    lfCmd("""call win_execute(%d, "let matchid = matchaddpos('Lf_hl_matchRefine', %s)")"""
                            % (self._getInstance().getPopupWinId(), str(pos[j:j+8])))
                    id = int(lfEval("matchid"))
                else:
                    id = int(lfEval("matchaddpos('Lf_hl_matchRefine', %s)" % str(pos[j:j+8])))
                self._highlight_ids.append(id)

    def _regexFilter(self, iterable):
        def noErrMatch(text, pattern):
            try:
                return '-1' != lfEval("g:LfNoErrMsgMatch('%s', '%s')" % (text, pattern))
            except TypeError:   # python 2
                return '-1' != lfEval("g:LfNoErrMsgMatch('%s', '%s')" % (text.replace('\x00', '\x01'), pattern))
            except ValueError:  # python 3
                return '-1' != lfEval("g:LfNoErrMsgMatch('%s', '%s')" % (text.replace('\x00', '\x01'), pattern))
            except:
                return '-1' != lfEval("g:LfNoErrMsgMatch('%s', '%s')" % (text.replace('\x00', '\x01'), pattern))

        try:
            if ('-2' == lfEval("g:LfNoErrMsgMatch('', '%s')" % escQuote(self._cli.pattern))):
                return iter([])
            else:
                return (line for line in iterable
                        if noErrMatch(escQuote(self._getDigest(line, 0)), escQuote(self._cli.pattern)))
        except vim.error:
            return iter([])

    def _regexSearch(self, content, is_continue, step):
        if not is_continue and not self._cli.isPrefix:
            self._index = 0
        self._result_content = self._filter(8000, self._regexFilter, content, is_continue)
        self._getInstance().setBuffer(self._result_content[:self._initial_count])
        self._getInstance().setStlResultsCount(len(self._result_content), True)

    def clearSelections(self):
        for i in self._selections.values():
            if self._getInstance().getWinPos() == 'popup':
                lfCmd("call matchdelete(%d, %d)" % (i, self._getInstance().getPopupWinId()))
            else:
                lfCmd("call matchdelete(%d)" % i)
        self._selections.clear()

    def _cleanup(self):
        if not ("--recall" in self._arguments or lfEval("g:Lf_RememberLastSearch") == '1'):
            self._pattern_bak = self._cli.pattern
            self._cli.clear()
            self._clearHighlights()
            self._clearHighlightsPos()
            self._help_length = 0
            self._show_help = False

    @modifiableController
    def toggleHelp(self):
        self._show_help = not self._show_help
        if self._getInstance().isReverseOrder():
            if self._help_length > 0:
                del self._getInstance().buffer[-self._help_length:]
        else:
            del self._getInstance().buffer[:self._help_length]
            if self._help_length > 0 and self._getInstance().getWinPos() == 'popup':
                lfCmd("call win_execute(%d, 'norm! %dk')" % (self._getInstance().getPopupWinId(), self._help_length))
        self._createHelpHint()
        self.clearSelections()
        self._resetHighlights()

    @ignoreEvent('BufUnload')
    def _accept(self, file, mode, *args, **kwargs):
        if file:
            if self._getExplorer().getStlCategory() != "Jumps":
                lfCmd("norm! m'")

            if self._getExplorer().getStlCategory() != "Help":
                if mode == '':
                    pass
                elif mode == 'h':
                    lfCmd("split")
                elif mode == 'v':
                    lfCmd("bel vsplit")

            kwargs["mode"] = mode
            tabpage_count = len(vim.tabpages)
            self._acceptSelection(file, *args, **kwargs)
            for k, v in self._cursorline_dict.items():
                if k.valid:
                    k.options["cursorline"] = v
            self._cursorline_dict.clear()
            self._issue_422_set_option()
            if mode == 't' and len(vim.tabpages) > tabpage_count:
                tabmove()

    def accept(self, mode=''):
        if self._getInstance().isReverseOrder():
            if self._getInstance().window.cursor[0] > len(self._getInstance().buffer) - self._help_length:
                lfCmd("norm! k")
                return
        else:
            if self._getInstance().window.cursor[0] <= self._help_length:
                if self._getInstance().getWinPos() == 'popup':
                    lfCmd("call win_execute({}, 'norm! j')".format(self._getInstance().getPopupWinId()))
                else:
                    lfCmd("norm! j")

                if self._getInstance().getWinPos() in ('popup', 'floatwin'):
                    self._cli.buildPopupPrompt()

                return

        if self._getExplorer().getStlCategory() == "Rg":
            if self._getInstance().currentLine == self._getExplorer().getContextSeparator():
                return
            if "--heading" in self._arguments and not re.match(r'^\d+[:-]', self._getInstance().currentLine):
                return

        self._cli.writeHistory(self._getExplorer().getStlCategory())

        # https://github.com/neovim/neovim/issues/8336
        if lfEval("has('nvim')") == '1':
            chdir = vim.chdir
        else:
            chdir = os.chdir

        cwd = lfGetCwd()
        cwd = lfEval("getcwd()")
        if len(self._selections) > 0:
            files = []
            for i in sorted(self._selections.keys()):
                files.append(self._getInstance().buffer[i-1])
            if "--stayOpen" in self._arguments:
                if self._getInstance().window.valid:
                    self._getInstance().cursorRow = self._getInstance().window.cursor[0]
                self._getInstance().helpLength = self._help_length
                try:
                    if self._preview_winid:
                        self._closePreviewPopup()
                    vim.current.tabpage, vim.current.window, vim.current.buffer = self._getInstance().getOriginalPos()
                except vim.error: # error if original buffer is an No Name buffer
                    pass
            else:
                self._getInstance().exitBuffer()

            # https://github.com/Yggdroot/LeaderF/issues/257
            win_local_cwd = lfEval("getcwd()")
            if cwd != win_local_cwd:
                chdir(cwd)

            orig_cwd = lfGetCwd()
            if mode == '' and self._getExplorer().getStlCategory() == "File":
                self._accept(files[0], mode)
                self._argaddFiles(files)
                self._accept(files[0], mode)
                lfCmd("doautocmd BufwinEnter")
            else:
                for file in files:
                    self._accept(file, mode)

            if lfGetCwd() != orig_cwd:
                dir_changed_by_autocmd = True
            else:
                dir_changed_by_autocmd = False

            need_exit = True
        else:
            file = self._getInstance().currentLine
            line_num = self._getInstance().window.cursor[0]
            need_exit = self._needExit(file, self._arguments)
            if need_exit:
                if "--stayOpen" in self._arguments:
                    if self._getInstance().window.valid:
                        self._getInstance().cursorRow = self._getInstance().window.cursor[0]
                    self._getInstance().helpLength = self._help_length
                    try:
                        if self._preview_winid:
                            self._closePreviewPopup()
                        vim.current.tabpage, vim.current.window, vim.current.buffer = self._getInstance().getOriginalPos()
                    except vim.error: # error if original buffer is an No Name buffer
                        pass
                else:
                    self._getInstance().exitBuffer()

            # https://github.com/Yggdroot/LeaderF/issues/257
            win_local_cwd = lfEval("getcwd()")
            if cwd != win_local_cwd:
                chdir(cwd)

            orig_cwd = lfGetCwd()
            self._accept(file, mode, self._getInstance().buffer, line_num) # for bufTag
            if lfGetCwd() != orig_cwd:
                dir_changed_by_autocmd = True
            else:
                dir_changed_by_autocmd = False

        if need_exit:
            self._setAutochdir()
            if dir_changed_by_autocmd == False:
                self._restoreOrigCwd()
            return None
        else:
            self._beforeExit()
            self._content = vim.current.buffer[:]
            return False

    def _jumpNext(self):
        instance = self._getInstance()
        if instance.window is None or instance.empty() or len(instance.buffer) == self._help_length:
            return False

        if instance.isReverseOrder():
            if instance.window.valid:
                if instance.window.cursor[0] > len(instance.buffer) - self._help_length:
                    instance.window.cursor = (len(instance.buffer) - self._help_length, 0)
                elif instance.window.cursor[0] == 1: # at the first line
                    instance.window.cursor = (len(instance.buffer) - self._help_length, 0)
                else:
                    instance.window.cursor = (instance.window.cursor[0] - 1, 0)
                instance.window.options["cursorline"] = True

                instance.gotoOriginalWindow()
                line_num = self._getInstance().window.cursor[0]
                self._accept(instance.buffer[instance.window.cursor[0] - 1], "", self._getInstance().buffer, line_num)
            else:
                if instance.cursorRow > len(instance.buffer) - instance.helpLength:
                    instance.cursorRow = len(instance.buffer) - instance.helpLength
                    line_num = instance.cursorRow
                elif instance.cursorRow == 1: # at the last line
                    line_num = instance.cursorRow
                    instance.cursorRow = len(instance.buffer) - instance.helpLength
                else:
                    line_num = instance.cursorRow
                    instance.cursorRow -= 1

                self._accept(instance.buffer[instance.cursorRow - 1], "", self._getInstance().buffer, line_num)
                lfCmd("echohl WarningMsg | redraw | echo ' (%d of %d)' | echohl NONE"
                        % (len(instance.buffer) - instance.cursorRow - instance.helpLength + 1,
                            len(instance.buffer) - instance.helpLength))
        else:
            if instance.window.valid and self._getInstance().getWinPos() != 'popup':
                if instance.window.cursor[0] <= self._help_length:
                    instance.window.cursor = (self._help_length + 1, 0)
                elif instance.window.cursor[0] == len(instance.buffer): # at the last line
                    instance.window.cursor = (self._help_length + 1, 0)
                else:
                    instance.window.cursor = (instance.window.cursor[0] + 1, 0)
                instance.window.options["cursorline"] = True

                instance.gotoOriginalWindow()
                line_num = self._getInstance().window.cursor[0]
                self._accept(instance.buffer[instance.window.cursor[0] - 1], "", self._getInstance().buffer, line_num)
            else:
                if instance.cursorRow <= instance.helpLength:
                    instance.cursorRow = instance.helpLength + 1
                    line_num = instance.cursorRow
                elif instance.cursorRow == len(instance.buffer): # at the last line
                    line_num = instance.cursorRow
                    instance.cursorRow = instance.helpLength + 1
                else:
                    line_num = instance.cursorRow
                    instance.cursorRow += 1

                self._accept(instance.buffer[instance.cursorRow - 1], "", self._getInstance().buffer, line_num)
                lfCmd("echohl WarningMsg | redraw | echo ' (%d of %d)' | echohl NONE" % \
                        (instance.cursorRow - instance.helpLength, len(instance.buffer) - instance.helpLength))

        return True

    def _jumpPrevious(self):
        instance = self._getInstance()
        if instance.window is None or instance.empty() or len(instance.buffer) == self._help_length:
            return False

        if instance.isReverseOrder():
            if instance.window.valid:
                if instance.window.cursor[0] >= len(instance.buffer) - self._help_length:
                    instance.window.cursor = (1, 0)
                else:
                    instance.window.cursor = (instance.window.cursor[0] + 1, 0)
                instance.window.options["cursorline"] = True

                instance.gotoOriginalWindow()
                line_num = self._getInstance().window.cursor[0]
                self._accept(instance.buffer[instance.window.cursor[0] - 1], "", self._getInstance().buffer, line_num)
            else:
                if instance.cursorRow >= len(instance.buffer) - instance.helpLength:
                    instance.cursorRow = 1
                    line_num = instance.cursorRow
                else:
                    line_num = instance.cursorRow
                    instance.cursorRow += 1

                self._accept(instance.buffer[instance.cursorRow - 1], "", self._getInstance().buffer, line_num)
                lfCmd("echohl WarningMsg | redraw | echo ' (%d of %d)' | echohl NONE"
                        % (len(instance.buffer) - instance.cursorRow - instance.helpLength + 1,
                            len(instance.buffer) - instance.helpLength))
        else:
            if instance.window.valid and self._getInstance().getWinPos() != 'popup':
                if instance.window.cursor[0] <= self._help_length + 1:
                    instance.window.cursor = (len(instance.buffer), 0)
                else:
                    instance.window.cursor = (instance.window.cursor[0] - 1, 0)
                instance.window.options["cursorline"] = True

                instance.gotoOriginalWindow()
                line_num = self._getInstance().window.cursor[0]
                self._accept(instance.buffer[instance.window.cursor[0] - 1], "", self._getInstance().buffer, line_num)
            else:
                if instance.cursorRow <= instance.helpLength + 1:
                    instance.cursorRow = len(instance.buffer)
                    line_num = instance.cursorRow
                else:
                    line_num = instance.cursorRow
                    instance.cursorRow -= 1

                self._accept(instance.buffer[instance.cursorRow - 1], "", self._getInstance().buffer, line_num)
                lfCmd("echohl WarningMsg | redraw | echo ' (%d of %d)' | echohl NONE" % \
                        (instance.cursorRow - instance.helpLength, len(instance.buffer) - instance.helpLength))

    def quit(self):
        self._getInstance().exitBuffer()
        self._setAutochdir()
        self._restoreOrigCwd()

    def refresh(self, normal_mode=True):
        self._getExplorer().cleanup()
        content = self._getExplorer().getFreshContent()
        if not content:
            lfCmd("echohl Error | redraw | echo ' No content!' | echohl NONE")
            return

        if normal_mode: # when called in Normal mode
            self._getInstance().buffer.options['modifiable'] = True

        self._clearHighlights()
        self._clearHighlightsPos()
        self.clearSelections()

        self._content = self._getInstance().initBuffer(content, self._getUnit(), self._getExplorer().setContent)
        self._iteration_end = True

        if self._cli.pattern:
            self._index = 0
            self._search(self._content)

        if normal_mode: # when called in Normal mode
            self._createHelpHint()
            self._resetHighlights()
            self._getInstance().buffer.options['modifiable'] = False

    def addSelections(self):
        nr = self._getInstance().window.number

        if self._getInstance().getWinPos() != 'popup':
            if (int(lfEval("v:mouse_win")) != 0 and
                    nr != int(lfEval("v:mouse_win"))):
                return
            elif nr == int(lfEval("v:mouse_win")):
                lfCmd("exec v:mouse_lnum")
                lfCmd("exec 'norm!'.v:mouse_col.'|'")

        line_num = self._getInstance().window.cursor[0]
        if self._getInstance().isReverseOrder():
            if line_num > len(self._getInstance().buffer) - self._help_length:
                lfCmd("norm! k")
                return
        else:
            if line_num <= self._help_length:
                if self._getInstance().getWinPos() == 'popup':
                    lfCmd("call win_execute({}, 'norm! j')".format(self._getInstance().getPopupWinId()))
                else:
                    lfCmd("norm! j")

                if self._getInstance().getWinPos() in ('popup', 'floatwin'):
                    self._cli.buildPopupPrompt()

                return

        if line_num in self._selections:
            if self._getInstance().getWinPos() == 'popup':
                lfCmd("call matchdelete(%d, %d)" % (self._selections[line_num], self._getInstance().getPopupWinId()))
            else:
                lfCmd("call matchdelete(%d)" % self._selections[line_num])
            del self._selections[line_num]
        else:
            if self._getInstance().getWinPos() == 'popup':
                lfCmd("""call win_execute(%d, "let matchid = matchadd('Lf_hl_selection', '\\\\%%%dl.')")"""
                        % (self._getInstance().getPopupWinId(), line_num))
                id = int(lfEval("matchid"))
            else:
                id = int(lfEval(r"matchadd('Lf_hl_selection', '\%%%dl.')" % line_num))
            self._selections[line_num] = id

    def selectMulti(self):
        orig_line = self._getInstance().window.cursor[0]
        nr = self._getInstance().window.number
        if (int(lfEval("v:mouse_win")) != 0 and
                nr != int(lfEval("v:mouse_win"))):
            return
        elif nr == int(lfEval("v:mouse_win")):
            cur_line = int(lfEval("v:mouse_lnum"))
        self.clearSelections()
        for i in range(min(orig_line, cur_line), max(orig_line, cur_line)+1):
            if i > self._help_length and i not in self._selections:
                id = int(lfEval(r"matchadd('Lf_hl_selection', '\%%%dl.')" % (i)))
                self._selections[i] = id

    def selectAll(self):
        line_num = len(self._getInstance().buffer)
        if line_num > 300:
            lfCmd("echohl Error | redraw | echo ' Too many files selected!' | echohl NONE")
            lfCmd("sleep 1")
            return
        for i in range(line_num):
            if i >= self._help_length and i+1 not in self._selections:
                if self._getInstance().getWinPos() == 'popup':
                    lfCmd("""call win_execute(%d, "let matchid = matchadd('Lf_hl_selection', '\\\\%%%dl.')")"""
                            % (self._getInstance().getPopupWinId(), i+1))
                    id = int(lfEval("matchid"))
                else:
                    id = int(lfEval(r"matchadd('Lf_hl_selection', '\%%%dl.')" % (i+1)))
                self._selections[i+1] = id

    def _gotoFirstLine(self):
        if self._getInstance().getWinPos() == 'popup':
            lfCmd("call win_execute({}, 'norm! gg')".format(self._getInstance().getPopupWinId()))
        else:
            lfCmd("normal! gg")

    def _readFinished(self):
        pass

    def _previewFirstLine(self):
        time.sleep(0.002)
        if len(self._content) > 0:
            first_line = self._content[0]
            self._getInstance().setBuffer([first_line])
            self._previewResult(False)
            self._getInstance().clearBuffer()

    def startExplorer(self, win_pos, *args, **kwargs):
        arguments_dict = kwargs.get("arguments", {})
        if "--recall" in arguments_dict:
            self._arguments.update(arguments_dict)
        elif "--previous" in arguments_dict:
            self._arguments["--previous"] = arguments_dict["--previous"]
        elif "--next" in arguments_dict:
            self._arguments["--next"] = arguments_dict["--next"]
        else:
            self.setArguments(arguments_dict)

        self._getInstance().setArguments(self._arguments)
        self._cli.setArguments(self._arguments)
        self._cli.setNameOnlyFeature(self._getExplorer().supportsNameOnly())
        self._cli.setRefineFeature(self._supportsRefine())
        self._orig_line = None

        if self._getExplorer().getStlCategory() in ["Gtags"]:
            if "--update" in self._arguments or "--remove" in self._arguments:
                self._getExplorer().getContent(*args, **kwargs)
                return

        if "--next" in arguments_dict:
            if self._jumpNext() == False:
                lfCmd("echohl Error | redraw | echo 'Error, no content!' | echohl NONE")
            return
        elif "--previous" in arguments_dict:
            if self._jumpPrevious() == False:
                lfCmd("echohl Error | redraw | echo 'Error, no content!' | echohl NONE")
            return

        self._cleanup()

        # lfCmd("echohl WarningMsg | redraw | echo ' searching ...' | echohl NONE")
        empty_query = self._empty_query and self._getExplorer().getStlCategory() in ["File"]
        remember_last_status = "--recall" in self._arguments \
                or lfEval("g:Lf_RememberLastSearch") == '1' and self._cli.pattern
        if remember_last_status:
            content = self._content
            self._getInstance().useLastReverseOrder()
            win_pos = self._getInstance().getWinPos()
        else:
            content = self._getExplorer().getContent(*args, **kwargs)
            self._getInstance().setCwd(lfGetCwd())
            if self.autoJump(content) == True:
                return

            self._index = 0
            pattern = arguments_dict.get("--input", [""])[0]
            if len(pattern) > 1 and (pattern[0] == '"' and pattern[-1] == '"'
                    or pattern[0] == "'" and pattern[-1] == "'"):
                pattern = pattern[1:-1]
            self._cli.setPattern(pattern)
            self._result_content = []
            self._cb_content = []

        if content is None:
            return

        if not content:
            lfCmd("echohl Error | redraw | echo ' No content!' | echohl NONE")
            return

        # clear the buffer only when the content is not a list
        self._getInstance().enterBuffer(win_pos, not isinstance(content, list))
        self._initial_count = self._getInstance().getInitialWinHeight()

        self._getInstance().setStlCategory(self._getExplorer().getStlCategory())
        self._setStlMode(**kwargs)
        self._getInstance().setStlCwd(self._getExplorer().getStlCurDir())

        if kwargs.get('bang', 0):
            self._current_mode = 'NORMAL'
        else:
            self._current_mode = 'INPUT'
        lfCmd("call leaderf#colorscheme#popup#hiMode('%s', '%s')"
                % (self._getExplorer().getStlCategory(), self._current_mode))

        self._getInstance().setPopupStl(self._current_mode)

        if not remember_last_status:
            self._gotoFirstLine()

        self._start_time = time.time()
        self._bang_start_time = self._start_time
        self._bang_count = 0

        self._getInstance().buffer.vars['Lf_category'] = self._getExplorer().getStlCategory()

        self._read_content_exception = None
        if isinstance(content, list):
            self._is_content_list = True
            self._read_finished = 2

            if not remember_last_status:
                if len(content[0]) == len(content[0].rstrip("\r\n")):
                    self._content = content
                else:
                    self._content = [line.rstrip("\r\n") for line in content]

                self._getInstance().setStlTotal(len(self._content)//self._getUnit())
                self._getInstance().setStlResultsCount(len(self._content))
                if not empty_query:
                    self._getInstance().setBuffer(self._content[:self._initial_count])
                    self._previewResult(False)
            else:
                self._previewResult(False)

            if lfEval("has('nvim')") == '1':
                lfCmd("redrawstatus")
            self._callback = self._workInIdle
            if not kwargs.get('bang', 0):
                self._readFinished()
                self.input()
            else:
                if not remember_last_status and not empty_query:
                    self._getInstance().appendBuffer(self._content[self._initial_count:])
                elif remember_last_status and len(self._getInstance().buffer) < len(self._result_content):
                    self._getInstance().appendBuffer(self._result_content[self._initial_count:])

                lfCmd("echo")
                if self._cli.pattern:
                    self._cli._buildPrompt()
                self._getInstance().buffer.options['modifiable'] = False
                self._bangEnter()
                self._getInstance().mimicCursor()

                if not remember_last_status and not self._cli.pattern and empty_query:
                    self._gotoFirstLine()
                    self._guessSearch(self._content)
                    if self._result_content: # self._result_content is [] only if
                                             #  self._cur_buffer.name == '' or self._cur_buffer.options["buftype"] not in [b'', '']:
                        self._getInstance().appendBuffer(self._result_content[self._initial_count:])
                    else:
                        self._getInstance().appendBuffer(self._content[self._initial_count:])

                    if self._timer_id is not None:
                        lfCmd("call timer_stop(%s)" % self._timer_id)
                        self._timer_id = None

                self._bangReadFinished()

                lfCmd("echohl WarningMsg | redraw | echo ' Done!' | echohl NONE")
        elif isinstance(content, AsyncExecutor.Result):
            self._is_content_list = False
            self._callback = self._workInIdle
            if lfEval("get(g:, 'Lf_NoAsync', 0)") == '1':
                self._content = self._getInstance().initBuffer(content, self._getUnit(), self._getExplorer().setContent)
                self._previewResult(False)
                self._read_finished = 1
                self._offset_in_content = 0
            else:
                if self._getExplorer().getStlCategory() in ["Rg", "Gtags"]:
                    if "--append" in self.getArguments():
                        self._offset_in_content = len(self._content)
                        if self._pattern_bak:
                            self._getInstance().setBuffer(self._content, need_copy=False)
                            self._createHelpHint()
                    else:
                        self._content = []
                        self._offset_in_content = 0
                else:
                    self._content = []
                    self._offset_in_content = 0

                self._read_finished = 0

                self._stop_reader_thread = False
                self._reader_thread = threading.Thread(target=self._readContent, args=(content,))
                self._reader_thread.daemon = True
                self._reader_thread.start()
                if not self._getInstance().isReverseOrder():
                    self._previewFirstLine()

            if not kwargs.get('bang', 0):
                self.input()
            else:
                lfCmd("echo")
                self._getInstance().buffer.options['modifiable'] = False
                self._bangEnter()
                self._getInstance().mimicCursor()
        else:
            self._is_content_list = False
            self._callback = partial(self._workInIdle, content)
            if lfEval("get(g:, 'Lf_NoAsync', 0)") == '1':
                self._content = self._getInstance().initBuffer(content, self._getUnit(), self._getExplorer().setContent)
                self._previewResult(False)
                self._read_finished = 1
                self._offset_in_content = 0
            else:
                self._content = []
                self._offset_in_content = 0
                self._read_finished = 0

            if not kwargs.get('bang', 0):
                self.input()
            else:
                lfCmd("echo")
                self._getInstance().buffer.options['modifiable'] = False
                self._bangEnter()
                self._getInstance().mimicCursor()

    def _readContent(self, content):
        try:
            for line in content:
                self._content.append(line)
                if self._stop_reader_thread:
                    break
            else:
                self._read_finished = 1
        except Exception:
            self._read_finished = 1
            self._read_content_exception = sys.exc_info()

    def _setResultContent(self):
        if len(self._result_content) > len(self._getInstance().buffer):
            self._getInstance().setBuffer(self._result_content)
        elif self._index == 0:
            self._getInstance().setBuffer(self._content, need_copy=True)

    @catchException
    def _workInIdle(self, content=None, bang=False):
        if self._read_content_exception is not None:
            if bang == True:
                if self._timer_id is not None:
                    lfCmd("call timer_stop(%s)" % self._timer_id)
                    self._timer_id = None

                lfPrintError(self._read_content_exception[1])
                return None
            else:
                raise self._read_content_exception[1]

        if self._is_content_list:
            if self._cli.pattern and (self._index < len(self._content) or len(self._cb_content) > 0):
                if self._fuzzy_engine:
                    step = 60000 * cpu_count
                elif is_fuzzyMatch_C:
                    step = 10000
                else:
                    step = 2000
                self._search(self._content, True, step)
                return None
            else:
                return 100

        if content:
            i = -1
            for i, line in enumerate(itertools.islice(content, 20)):
                self._content.append(line)

            if i == -1 and self._read_finished == 0:
                self._read_finished = 1

        if self._read_finished > 0:
            if self._read_finished == 1:
                self._read_finished += 1
                self._getExplorer().setContent(self._content)
                self._getInstance().setStlTotal(len(self._content)//self._getUnit())
                self._getInstance().setStlRunning(False)

                if self._cli.pattern:
                    self._getInstance().setStlResultsCount(len(self._result_content))
                elif self._empty_query and self._getExplorer().getStlCategory() in ["File"]:
                    self._guessSearch(self._content)
                    if bang:
                        if self._result_content: # self._result_content is [] only if
                                                 #  self._cur_buffer.name == '' or self._cur_buffer.options["buftype"] != b'':
                            self._getInstance().appendBuffer(self._result_content[self._initial_count:])
                        else:
                            self._getInstance().appendBuffer(self._content[self._initial_count:])

                        if self._timer_id is not None:
                            lfCmd("call timer_stop(%s)" % self._timer_id)
                            self._timer_id = None

                        self._bangReadFinished()

                        lfCmd("echohl WarningMsg | redraw | echo ' Done!' | echohl NONE")
                else:
                    if bang:
                        if self._getInstance().empty():
                            self._offset_in_content = len(self._content)
                            if self._offset_in_content > 0:
                                self._getInstance().appendBuffer(self._content[:self._offset_in_content])
                        else:
                            cur_len = len(self._content)
                            if cur_len > self._offset_in_content:
                                self._getInstance().appendBuffer(self._content[self._offset_in_content:cur_len])
                                self._offset_in_content = cur_len

                        if self._timer_id is not None:
                            lfCmd("call timer_stop(%s)" % self._timer_id)
                            self._timer_id = None

                        self._bangReadFinished()

                        lfCmd("echohl WarningMsg | redraw | echo ' Done!' | echohl NONE")
                    else:
                        self._getInstance().setBuffer(self._content[:self._initial_count])

                    self._getInstance().setStlResultsCount(len(self._content))

                if not self.isPreviewWindowOpen():
                    self._previewResult(False)

                if self._getInstance().getWinPos() not in ('popup', 'floatwin'):
                    lfCmd("redrawstatus")

            if self._cli.pattern:
                if self._index < len(self._content) or len(self._cb_content) > 0:
                    if self._fuzzy_engine:
                        step = 60000 * cpu_count
                    elif is_fuzzyMatch_C:
                        step = 10000
                    else:
                        step = 2000
                    self._search(self._content, True, step)

                    if bang:
                        self._getInstance().appendBuffer(self._result_content[self._initial_count:])
                else:
                    return 100
            else:
                return 100
        else:
            cur_len = len(self._content)
            if time.time() - self._start_time > 0.1:
                self._start_time = time.time()
                self._getInstance().setStlTotal(cur_len//self._getUnit())
                self._getInstance().setStlRunning(True)

                if self._cli.pattern:
                    self._getInstance().setStlResultsCount(len(self._result_content))
                else:
                    self._getInstance().setStlResultsCount(cur_len)

                if self._getInstance().getWinPos() not in ('popup', 'floatwin'):
                    lfCmd("redrawstatus")

            if self._cli.pattern:
                if self._index < cur_len or len(self._cb_content) > 0:
                    if self._fuzzy_engine:
                        step = 60000 * cpu_count
                    elif is_fuzzyMatch_C:
                        step = 10000
                    else:
                        step = 2000
                    self._search(self._content[:cur_len], True, step)
            else:
                if bang:
                    if self._getInstance().empty():
                        self._offset_in_content = len(self._content)
                        if self._offset_in_content > 0:
                            self._getInstance().appendBuffer(self._content[:self._offset_in_content])
                    else:
                        cur_len = len(self._content)
                        if cur_len > self._offset_in_content:
                            self._getInstance().appendBuffer(self._content[self._offset_in_content:cur_len])
                            self._offset_in_content = cur_len

                    if self._getInstance().getWinPos() not in ('popup', 'floatwin') \
                            and time.time() - self._bang_start_time > 0.5:
                        self._bang_start_time = time.time()
                        lfCmd("echohl WarningMsg | redraw | echo ' searching %s' | echohl NONE" % ('.' * self._bang_count))
                        self._bang_count = (self._bang_count + 1) % 9
                elif len(self._getInstance().buffer) < min(cur_len, self._initial_count):
                    self._getInstance().setBuffer(self._content[:self._initial_count])

                if not self.isPreviewWindowOpen():
                    self._previewResult(False)

        return None


    @modifiableController
    def input(self):
        self._current_mode = 'INPUT'
        self._getInstance().hideMimicCursor()
        if self._getInstance().getWinPos() in ('popup', 'floatwin'):
            self._cli.buildPopupPrompt()
            lfCmd("call leaderf#colorscheme#popup#hiMode('%s', '%s')"
                    % (self._getExplorer().getStlCategory(), self._current_mode))
            self._getInstance().setPopupStl(self._current_mode)

        if self._getInstance().getWinPos() == 'popup':
            lfCmd("call leaderf#ResetPopupOptions(%d, 'filter', '%s')"
                    % (self._getInstance().getPopupWinId(), 'leaderf#PopupFilter'))

        if self._timer_id is not None:
            lfCmd("call timer_stop(%s)" % self._timer_id)
            self._timer_id = None

        self.clearSelections()
        self._hideHelp()
        self._resetHighlights()

        # --input xxx or from normal mode to input mode
        if self._cli.pattern and "--live" not in self._arguments:
            if self._index == 0: # --input xxx
                self._search(self._content)
        elif self._empty_query and self._getExplorer().getStlCategory() in ["File"] \
                and "--recall" not in self._arguments:
            self._guessSearch(self._content)
            self._previewResult(False)

        for cmd in self._cli.input(self._callback):
            cur_len = len(self._content)
            cur_content = self._content[:cur_len]
            if equal(cmd, '<Update>'):
                if self._getInstance().getWinPos() == 'popup':
                    if self._getInstance()._window_object.cursor[0] > 1:
                        lfCmd("call win_execute({}, 'norm! gg')".format(self._getInstance().getPopupWinId()))
                self._search(cur_content)
            elif equal(cmd, '<Shorten>'):
                if self._getInstance().isReverseOrder():
                    lfCmd("normal! G")
                else:
                    self._gotoFirstLine()
                self._index = 0 # search from beginning
                self._search(cur_content)
            elif equal(cmd, '<Mode>'):
                self._setStlMode()
                if self._getInstance().getWinPos() in ('popup', 'floatwin'):
                    self._getInstance().setPopupStl(self._current_mode)

                if self._getInstance().isReverseOrder():
                    lfCmd("normal! G")
                else:
                    self._gotoFirstLine()
                self._index = 0 # search from beginning
                if self._cli.pattern and "--live" not in self._arguments:
                    self._search(cur_content)
            elif equal(cmd, '<C-K>'):
                self._toUp()
                self._previewResult(False)
            elif equal(cmd, '<C-J>'):
                self._toDown()
                self._previewResult(False)
            elif equal(cmd, '<Up>'):
                if self._cli.previousHistory(self._getExplorer().getStlCategory()):
                    if self._getInstance().isReverseOrder():
                        lfCmd("normal! G")
                    else:
                        self._gotoFirstLine()
                    self._index = 0 # search from beginning
                    self._search(cur_content)
            elif equal(cmd, '<Down>'):
                if self._cli.nextHistory(self._getExplorer().getStlCategory()):
                    if self._getInstance().isReverseOrder():
                        lfCmd("normal! G")
                    else:
                        self._gotoFirstLine()
                    self._index = 0 # search from beginning
                    self._search(cur_content)
            elif equal(cmd, '<LeftMouse>'):
                if self._leftClick():
                    break
                self._previewResult(False)
            elif equal(cmd, '<2-LeftMouse>'):
                self._leftClick()
                if self.accept() is None:
                    break
            elif equal(cmd, '<CR>'):
                if self.accept() is None:
                    break
            elif equal(cmd, '<C-X>'):
                if self.accept('h') is None:
                    break
            elif equal(cmd, '<C-]>'):
                if self.accept('v') is None:
                    break
            elif equal(cmd, '<C-T>'):
                if self.accept('t') is None:
                    break
            elif equal(cmd, r'<C-\>'):
                actions = ['', 'h', 'v', 't', 'dr']
                action_count = len(actions)
                selection = int( vim.eval(
                    'confirm("Action?", "&Edit\n&Split\n&Vsplit\n&Tabnew\n&Drop")' ) ) - 1
                if selection < 0 or selection >= action_count:
                    selection = 0
                action = actions[selection]
                if self.accept(action) is None:
                    break
            elif equal(cmd, '<Quit>'):
                self._cli.writeHistory(self._getExplorer().getStlCategory())
                self.quit()
                break
            elif equal(cmd, '<Tab>'):   # switch to Normal mode
                self._current_mode = 'NORMAL'
                if self._getInstance().getWinPos() == 'popup':
                    if lfEval("exists('*leaderf#%s#NormalModeFilter')" % self._getExplorer().getStlCategory()) == '1':
                        lfCmd("call leaderf#ResetPopupOptions(%d, 'filter', '%s')" % (self._getInstance().getPopupWinId(),
                                'leaderf#%s#NormalModeFilter' % self._getExplorer().getStlCategory()))
                    else:
                        lfCmd("call leaderf#ResetPopupOptions(%d, 'filter', function('leaderf#NormalModeFilter', [%d]))"
                                % (self._getInstance().getPopupWinId(), id(self)))

                self._setResultContent()
                self.clearSelections()
                self._cli.hideCursor()
                self._createHelpHint()
                self._resetHighlights()
                if self._getInstance().isReverseOrder() and self._cli.pattern \
                        and len(self._highlight_pos) < (len(self._getInstance().buffer) - self._help_length) // self._getUnit() \
                        and len(self._highlight_pos) < int(lfEval("g:Lf_NumberOfHighlight")):
                    self._highlight_method()

                if self._getInstance().getWinPos() in ('popup', 'floatwin'):
                    self._cli.buildPopupPrompt()
                    lfCmd("call leaderf#colorscheme#popup#hiMode('%s', '%s')"
                            % (self._getExplorer().getStlCategory(), self._current_mode))
                    self._getInstance().setPopupStl(self._current_mode)

                break
            elif equal(cmd, '<F5>'):
                self.refresh(False)
            elif equal(cmd, '<C-LeftMouse>') or equal(cmd, '<C-S>'):
                if self._getExplorer().supportsMulti():
                    self.addSelections()
            elif equal(cmd, '<S-LeftMouse>'):
                if self._getExplorer().supportsMulti():
                    self.selectMulti()
            elif equal(cmd, '<C-A>'):
                if self._getExplorer().supportsMulti():
                    self.selectAll()
            elif equal(cmd, '<C-L>'):
                self.clearSelections()
            elif equal(cmd, '<C-P>'):
                self._previewResult(True)
            elif equal(cmd, '<PageUp>'):
                self._pageUp()
                self._previewResult(False)
            elif equal(cmd, '<PageDown>'):
                self._pageDown()
                self._previewResult(False)
            elif equal(cmd, '<C-Up>'):
                self._toUpInPopup()
            elif equal(cmd, '<C-Down>'):
                self._toDownInPopup()
            elif equal(cmd, '<ScrollWheelUp>'):
                self._scrollUp()
                self._previewResult(False)
            elif equal(cmd, '<ScrollWheelDown>'):
                self._scrollDown()
                self._previewResult(False)
            elif equal(cmd, '<QuickSelect>'):
                if self._quickSelect():
                    break
            else:
                if self._cmdExtension(cmd):
                    break

#  vim: set ts=4 sw=4 tw=0 et :
