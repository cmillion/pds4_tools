from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import functools
import os
import platform
import subprocess
import sys
import traceback
import weakref

from . import cache
from ..utils.logging import logger_init

from ..extern import six
from ..extern.six.moves.tkinter import Event as TKEvent
from ..extern.six.moves.tkinter import (Tk, Toplevel, PhotoImage, Menu, Scrollbar, Canvas, Frame, Label,
                                        Entry, Text, Button, Checkbutton, BooleanVar, StringVar, TclError)

# NOTE: DO NOT IMPORT MATPLOTLIB HERE, import any other module
# that in-turn imports MPL or any module that imports anything that
# should not be imported when the Viewer is not actually used. See
# note in ``_mpl_commands`` below for explanation. This module is
# imported by `pds4_tools.__init__``, as is any module this module
# in-turn imports.

# Safe import of OrderedDict
try:
    from collections import OrderedDict
except ImportError:
    from ..extern.ordered_dict import OrderedDict

# Initialize the logger
logger = logger_init()

#################################


class PDS4Viewer(object):
    """ Main class and window manager for PDS4 Viewer

    PDS4 Viewer hides the root window, and uses additional Toplevel Windows to display its contents. Once all
    PDS4 Viewer open Windows are the closed the root window is then also closed automatically. Additionally
    the root and all other Windows can be closed anytime via .quit(). PDS4 Viewer will auto-remove any Window
    that is physically closed by the user.
    """

    def __init__(self, root):

        self._root = root
        self._root.withdraw()

        self._open_windows = []

        # Sets icon for any default TK windows (e.g. tkFileDialog)
        _set_icon(self._root)

        # Bind uncaught exception handling
        self._root.report_callback_exception = self._handle_exception

        # Allow select-all via Ctrl+A or Command+A for all text and entry widgets
        cmd = '<Command-a>' if (platform.system() == 'Darwin') else '<Control-a>'
        self._root.bind_class('Text', cmd, lambda event: event.widget.tag_add('sel', '1.0', 'end'))
        self._root.bind_class('Entry', cmd, lambda event: event.widget.selection_range(0, 'end'))

        # Temporary reset of MPL's rcparams to MPL defaults, such that custom settings do not
        # interfere with designed GUI look (set back to user defaults on quit)
        _mpl_commands('set_default_mpl_rcparams')

        # Mac localization
        if platform.system() == 'Darwin':

            # Do not open TK's default About screen (only Mac exposes it by default)
            self._root.createcommand('tkAboutDialog', lambda *args: None)

            # On Mac, we allow labels to be opened by dragging them onto the opened application.
            # Note: PyInstaller currently does not support argv emulation/system events
            # after application is open. Therefore this will not work and is commented out.
            # from .summary_view import open_summary
            #
            # def _mac_open_labels(*args):
            #     for filename in args:
            #         open_summary(self, filename=filename)
            #
            # self._root.createcommand("::tk::mac::OpenDocument", _mac_open_labels)

    # Adds window to PDS4 Viewer
    def add_window(self, window):

        # We use weakref ref because otherwise Python will not quickly garbage collect
        # the window when its closed since there is a circular reference between viewer
        # and window. Aside from being garbage collected easier, this object, once
        # dereferenced via (), is identical to the window and all its methods and attributes
        # can be used as usual
        window = weakref.ref(window)
        self._open_windows.append(window)

    # Removes window from PDS4 Viewer
    def remove_window(self, window):

        # We use weakref because on Python 2 weakref(obj) is not obj
        window = weakref.ref(window)
        self._open_windows.remove(window)

        if len(self._open_windows) == 0:
            self.quit()

    # Returns true if the passed in window is open, false otherwise
    def is_window_open(self, window):

        # We use weakref because on Python 2 weakref(obj) is not obj
        window = weakref.ref(window)
        if window in self._open_windows:
            return True

        return False

    # Quits PDS4 Viewer, closing all open windows
    def quit(self):

        # Under some circumstances destroying the root window will not close other windows, therefore
        # we do it ourselves (after dereferencing the window)
        for window in self._open_windows:
            window().close()

        # Under some circumstances both quit() and destroy() are necessary to kill the TK mainloop
        if self._root is not None:
            self._root.quit()
            self._root.destroy()
            self._root = None

        # Set MPL's defaults back to previously specified by user (prior to being reset in init above)
        _mpl_commands('restore_mpl_rcparams')

    # Catch unhandled exceptions, display them in GUI and log them
    def _handle_exception(self, exc_type=None, exc_value=None, exc_traceback=None):

        error = traceback.format_exc()
        MessageWindow(self, 'An Error Occurred!', error)
        logger.exception(exc_value)


class Window(object):
    """ Base class of any window for PDS4 Viewer """

    def __init__(self, viewer, withdrawn=True):

        # Initialize window as a Toplevel TK window
        window_name = 'PDS4 Viewer'
        self._widget = Toplevel(class_=window_name)
        self._set_title(window_name)

        # Immediately hide window if requested
        if withdrawn:
            self._hide_window()

        # Add window to window manager for tracking, and set its icon
        self._viewer = viewer
        self._viewer.add_window(self)
        _set_icon(self._widget)

        # Ensure that pressing the (usually red) close button is routed to `self.close`
        self._widget.protocol('WM_DELETE_WINDOW', self.close)

        # Initialize window dimensions (not automatically updated)
        self._win_dimensions = {'width': 0, 'height': 0}

        # Initialize required variables for windows
        self._menu = None
        self._menu_options = {}
        self._callbacks = {}
        self._dependent_windows = []

    # Closes the current window
    def close(self):

        # Process callbacks with name 'close', to cleanup anything that would prevent python from
        # immediately freeing memory on window close
        self._process_callbacks('close')

        # Close dependent windows (after derefencing them), iterating backwards since we modify
        # the list while iterating (on close garbage collection, the weakref callback is called
        # and deletes the window from the list)
        for i in range(len(self._dependent_windows) - 1, -1, -1):
                self._dependent_windows[i]().close()

        # If, somehow, a reference to a dependent window is retained even though it was closed [this should
        # really only happen on error, TK keeps a reference to the traceback which references the window,
        # which prevents it from being garbage collected], having this dependent window still be referenced
        # by `_dependent_windows` will prevent the parent window from being garbage collected no matter what.
        # Therefore we remove manually ensure that the list is empty after all dependent windows were closed.
        self._dependent_windows = []

        # Close this window
        if self._viewer.is_window_open(self):
            self._widget.destroy()
            self._viewer.remove_window(self)

    # Returns a menu option value given by name, if one exists, or None if it does not exist. If
    # `value` is set to false, the actual menu option TK variable will be returned.
    def menu_option(self, name, value=True):

        option = self._menu_options.get(name)

        if option is not None:

            if value:
                option_value = option.get()
            else:
                option_value = option.get()

        else:
            raise KeyError('Unknown menu option requested.')

        return option_value

    # Issues warning. If log is true, the warning is logged. If show is True, a warning message will open.
    def _issue_warning(self, message, title='Warning', log=True, show=True):

        warning_window = None

        if log:
            logger.warning(message)

        if show and not logger.is_quiet():
            warning_window = MessageWindow(self._viewer, title, message, word_wrap=True)

            # Attempt to make window on top of any others open
            try:
                warning_window._widget.wm_attributes("-topmost", 1)

            # Not available on some OS' and some TK versions
            except TclError:
                pass

            # Force update, such that the warning message is shown properly even if GUI then freezes
            self._widget.update()

        return warning_window

    # Adds a dependent window to be tracked (a dependent window is one that should be closed if the
    # parent window is closed. For example, if manipulating a window after its parent was closed will
    # cause an error then it should be a dependent window)
    def _add_dependent_window(self, window):

        # We use weakref ref because otherwise Python will not garbage collect the dependent
        # window when its closed since it is still in the dependent_windows list and nothing
        # ever deletes it from said list. By using weakref and a callback, we both allow it to
        # be garbage collected and remove it from the list. Aside from being a garbage collected
        # easier, this object can be derefenced via () and is then is identical to the window
        # and all its methods and attributes can be used as usual. Do not use a weakref proxy
        # here as it will create a ReferenceError when removing the window from the list
        window = weakref.ref(window, self._remove_dependent_window)
        self._dependent_windows.append(window)

    # Removes a dependent window from being tracked
    def _remove_dependent_window(self, window):
        self._dependent_windows.remove(window)

    # Adds a callback for func(*args, **kwargs).
    def _add_callback(self, name, func, *args, **kwargs):

        callback = functools.partial(func, *args, **kwargs)

        if name in self._callbacks:
            self._callbacks[name].append(callback)

        else:
            self._callbacks[name] = [callback]

    # Removes a specific callback if both name and func given, use ignore_args if you want to remove func
    # regardless of what arguments it originally had, or specify them via `args` and `kwargs`. Removes all
    # callbacks with name if only name is given, and removes all callbacks if name is not given. Does not
    # raise error if no callbacks with specified inputs are found
    def _remove_callbacks(self, name=None, func=None, ignore_args=False, *args, **kwargs):

        if name is not None:

            if func is not None:

                callbacks = self._callbacks.get(name, [])
                match_callback = functools.partial(func, *args, **kwargs)

                for i, callback in enumerate(callbacks):

                    if callback.func == match_callback.func:

                        if ignore_args:
                            callbacks.pop(i)

                        elif(callback.args == match_callback.args) and  \
                            (callback.keywords == match_callback.keywords):
                            callbacks.pop(i)

            else:
                self._callbacks.pop(name, [])

        else:
            self._callbacks = {}

    # Processes callback(s) with the given name, or all callbacks if name is not given. By default removes
    # callback(s) once it is processed.
    def _process_callbacks(self, name=None, with_removal=True):

        if name is None:

            for callbacks in six.itervalues(self._callbacks):

                for callback in callbacks:
                    callback()

            if with_removal:
                self._callbacks = {}

        else:

            callbacks = self._callbacks.get(name, [])

            for callback in callbacks:
                callback()

            if with_removal and len(callbacks) > 0:
                self._callbacks.pop(name, None)

    # Convenience wrapper around creating a trace for TK's variables. A trace uses a TK variable, and
    # automatically runs the callback when a mode action is done. Modes available are 'w', called when the
    # TK variable is written to, 'r' when a TK variable is read from and 'u' for when the variable is deleted
    def _add_trace(self, variable, mode, callback, default=None):

        if default is not None:
            variable.set(default)

        trace_id = variable.trace(mode, callback)

        # Create a callback to delete the trace, otherwise it will stay bound and prevent python
        # from clearing window memory on close
        self._add_callback('close', variable.trace_vdelete, 'w', trace_id)

        return trace_id

    # Adds a file menu
    def _add_file_menu(self):

        self._widget.config(menu=self._menu)

        file_menu = Menu(self._menu, tearoff=0)
        file_menu.add_command(label='Close', command=self.close)
        file_menu.add_command(label='Close All', command=self._viewer.quit)
        self._menu.add_cascade(label='File', menu=file_menu)

    # Obtain window title
    def _get_title(self):
        return self._widget.title()

    # Set window title
    def _set_title(self, title):
        self._widget.title(title)

    # Set window dimensions
    def _set_window_dimensions(self, width, height, x_offset=None, y_offset=None):

        self._win_dimensions = {'width': width, 'height': height}

        if (x_offset is not None) and (y_offset is not None):
            self._widget.geometry('{0}x{1}+{2}+{3}'.format(width, height, x_offset, y_offset))

        else:
            self._widget.geometry('{0}x{1}'.format(width, height))

    # Set window dimensions by adding to current dimensions
    def _add_to_window_dimensions(self, width=None, height=None):

        set_width = self._win_dimensions['width']
        set_height = self._win_dimensions['height']

        if width is not None:
            set_width += width

        if height is not None:
            set_height += height

        self._set_window_dimensions(set_width, set_height)

    def _update_window_dimensions(self):
        self._widget.update_idletasks()
        self._win_dimensions = {'width': self._widget.winfo_width(), 'height': self._widget.winfo_height()}

    # Center the window on the screen
    def _center_window(self):
        self._widget.update_idletasks()

        width = self._win_dimensions['width']
        height = self._win_dimensions['height']
        x_offset = (self._get_screen_size()[0] // 2) - (width // 2)
        y_offset = (self._get_screen_size()[1] // 2) - (height // 2)

        self._set_window_dimensions(width, height, x_offset, y_offset)

    # Set window size to exactly fit its content
    def _fit_to_content(self):
        self._widget.update_idletasks()
        self._set_window_dimensions(self._widget.winfo_reqwidth(), self._widget.winfo_reqheight())

    # Center the window on the screen and fit it to content (avoiding redrawing it twice)
    def _center_and_fit_to_content(self):
        self._widget.update_idletasks()

        width = self._widget.winfo_reqwidth()
        height = self._widget.winfo_reqheight()
        x_offset = (self._get_screen_size()[0] // 2) - (width // 2)
        y_offset = (self._get_screen_size()[1] // 2) - (height // 2)

        self._set_window_dimensions(width, height, x_offset, y_offset)

    # Adds a scroll event binding
    def _bind_scroll_event(self, scroll_method):

        if platform.system() == 'Linux':
            self._widget.bind('<Button-4>', lambda e: self._widget.event_generate('<MouseWheel>', delta=120))
            self._widget.bind('<Button-5>', lambda e: self._widget.event_generate('<MouseWheel>', delta=-120))

        self._widget.bind('<MouseWheel>', scroll_method)

    # Change window from normal state to withdrawn state
    def _hide_window(self):
        self._widget.withdraw()

    # Change window from withdrawn state to normal state
    def _show_window(self):
        self._widget.deiconify()

    # Returns the size of the primary screen, automatically corrected for cross-platform differences
    def _get_screen_size(self):

        # Get primary screen size on Windows and Mac
        screen_width = self._widget.winfo_screenwidth()
        screen_height = self._widget.winfo_screenheight()

        # Get primary screen size on Linux
        if platform.system() == 'Linux':

            # On Linux, `winfo_screenwidth` and `winfo_screenheight` return the total size of all
            # monitors combined into one. Therefore we use the below hack to reliably obtain the
            # size of only the single primary monitor.
            try:

                with open(os.devnull, 'w') as devnull:
                    xrandr_output = subprocess.check_output("xrandr  | grep \* | cut -d' ' -f4",
                                                            shell=True, stderr=devnull)

                if isinstance(xrandr_output, bytes):
                    xrandr_output == xrandr_output.decode('utf-8')

                monitor_sizes = xrandr_output.splitlines()
                primary_size = monitor_sizes[0].split('x')

                screen_width = int(primary_size[0])
                screen_height = int(primary_size[1])

            except Exception:
                pass

        return screen_width, screen_height

    # Returns the hex background color, automatically corrected for cross-platform differences
    def get_bg(self, type='transparent'):

        # Return transparent (for target system) background color
        if type == 'transparent':

            if (platform.system() == 'Windows') or (platform.system() == 'Darwin'):
                bg_name = 'SystemButtonFace'

            else:
                bg_name = self._widget.cget('background')

        # Return gray background color (this is the color for most TK widgets on Windows and Linux)
        elif type == 'gray':

            if platform.system() == 'Windows':
                bg_name = 'SystemButtonFace'

            elif platform.system() == 'Darwin':
                bg_name = '#F0F0F0'

            else:
                bg_name = self._widget.cget('background')

        else:
            raise ValueError('Unknown background name.')

        rgb = self._widget.winfo_rgb(bg_name)
        rgb = (rgb[0]/256, rgb[1]/256, rgb[2]/256)
        hex = '#{0:02x}{1:02x}{2:02x}'.format(*map(int, rgb))
        return hex

    # Returns a TK font string, automatically corrected for cross-platform differences
    @classmethod
    def get_font(cls, size=9, weight='', name='TkDefaultFont'):

        # Mac fonts seem to be 3 sizes too small compared to Windows and Linux
        if platform.system() == 'Darwin':
            size += 3

        # Courier as a monospace font looks best on Windows and Mac, but poorly on Linux
        if (platform.system() != 'Linux') and (name == 'monospace'):
            name = 'Courier'

        return '{0} {1} {2}'.format(name, size, weight)


class DataViewWindow(Window):
    """ Window for displaying a single data view; specific view windows (e.g. TabularViewWindow,
    ImageViewWindow, PlotViewWindow) extend this class """

    def __init__(self, viewer):

        # Set initial necessary variables and do other required initialization procedures
        super(DataViewWindow, self).__init__(viewer)

        # Create File Menu
        self._menu = Menu(self._widget)
        self._widget.config(menu=self._menu)

        file_menu = Menu(self._menu, tearoff=0)
        file_menu.add_command(label='Close', command=self.close)
        file_menu.add_command(label='Close All', command=self._viewer.quit)
        self._menu.add_cascade(label='File', menu=file_menu)

        # Initialize variables needed for any structure display window
        self.data = None
        self.meta_data = None
        self.structure = None
        self._warnings = ''
        self._settings = {}
        self._data_open = False

        # Initialize window size
        width = self._get_screen_size()[0] // 2
        height = self._get_screen_size()[1] // 2
        self._set_window_dimensions(width, height)
        self._center_window()

        # Add the header canvas, which contains the (positionally static) header information
        self._header = Canvas(self._widget, takefocus=False, bd=0, highlightthickness=0, height=0)
        self._header.pack(fill='x')

        # Create the canvas, which contains the main portion of the display
        self._canvas = Canvas(self._widget, takefocus=False, bd=0, highlightthickness=0)
        self._canvas.pack(expand=1, fill='both')

        # Create the display frame (inside the canvas), which contains the scrollbars and the static canvas
        self._display_frame = Frame(self._canvas, takefocus=False)

        # Create the static canvas (inside the display frame), which contains the scrollable canvas and
        # any other content that should be inside the scrollbars but positionally static
        self._static_canvas = Canvas(self._display_frame, takefocus=False, bd=0, highlightthickness=0)

        # Create the scrollable canvas (inside the static canvas), which contains the content that will
        # need to have the ability to be scrolled
        self._scrollable_canvas = Canvas(self._static_canvas, takefocus=False, bd=0, highlightthickness=0)

        # Create the footer canvas, which contains any data below the canvas (currently this is not used)
        # self._footer = Canvas(self._widget, takefocus=False, bd=0, highlightthickness=0, height=0)
        # self._footer.pack(fill='x')

        self._vert_scrollbar = None
        self._horz_scrollbar = None

        # Show window once all initialization is done
        self._show_window()

    @property
    def settings(self):
        return self._settings.copy()

    # Issues warning. If log is true, the warning is logged. If show is True, a warning message will open.
    def _issue_warning(self, message, title='Critical Warning', log=True, show=True):

        self._warnings += '{0} \n'.format(message)
        super(DataViewWindow, self)._issue_warning(message, title=title, log=log, show=show)

    # Adds a view menu
    def _add_view_menu(self):

        from . import label_view

        view_menu = Menu(self._menu, tearoff=0)
        self._menu.add_cascade(label='View', menu=view_menu)

        labels = [self.structure.full_label, self.structure.label] if self.structure else [None, None]
        disable_open_label = {} if all(labels) else {'state': 'disabled'}

        view_menu.add_command(label='Label', command=lambda: label_view.open_label(
            self._viewer, *labels, initial_display='object label'), **disable_open_label)

        view_menu.add_separator()

        view_menu.add_command(label='Warnings',
                              command=lambda: MessageWindow(self._viewer, 'Warnings', self._warnings))

        return view_menu

    def close(self):

        self.data = None
        self.meta_data = None
        self.structure = None

        super(DataViewWindow, self).close()


class MessageWindow(Window):
    """ Window used to display messages inside a scrollable text area """

    def __init__(self, viewer, message_header, message, word_wrap=False):

        # Set initial necessary variables and do other required initialization procedures
        super(MessageWindow, self).__init__(viewer)

        # Set a title for the window
        self._set_title('{0} - {1}'.format(self._get_title(), message_header))

        # Add header
        header_box = Frame(self._widget, bg=self.get_bg('gray'))
        header_box.pack(side='top', fill='x')

        header = Label(header_box, text=message_header, bg=self.get_bg('gray'), font=self.get_font(15, 'bold'))
        header.pack(pady=10)

        # Add buttons Ok and Copy buttons below the text pad first so they remain during resizing
        button_frame = Frame(self._widget)
        button_frame.pack(pady=10, side='bottom')

        ok_button = Button(button_frame, bg=self.get_bg(), width=5, text='Ok',
                           font=self.get_font(weight='bold'), command=self.close)
        ok_button.pack(side='left', padx=(0, 5))

        copy_button = Button(button_frame, bg=self.get_bg(), width=7, text='Copy',
                             font=self.get_font(weight='bold'), command=self._copy_message)
        copy_button.pack(side='left')

        # Add text pad
        self._text_pad = None
        self._create_text_pad(word_wrap)
        self._text_pad.insert('end', message)
        self._text_pad.config(state='disabled')
        self._text_pad.see('end-1c linestart')

        self._center_and_fit_to_content()
        self._show_window()

    def _create_text_pad(self, word_wrap):

        wrap = 'none'
        if word_wrap:
            wrap = 'word'

        frame = Frame(self._widget)
        self._text_pad = Text(frame, wrap=wrap, width=80, height=20, bg='white')

        vert_scrollbar = Scrollbar(frame, orient='vertical', command=self._text_pad.yview)
        self._text_pad.configure(yscrollcommand=vert_scrollbar.set)

        horz_scrollbar = Scrollbar(frame, orient='horizontal', command=self._text_pad.xview)
        self._text_pad.configure(xscrollcommand=horz_scrollbar.set)

        vert_scrollbar.pack(side='right', fill='y')
        horz_scrollbar.pack(side='bottom', fill='x')
        self._text_pad.pack(side='left', expand=2, fill='both')
        frame.pack(expand=2, fill='both')

    def _copy_message(self):
        message = self._text_pad.get('1.0', 'end-1c')

        self._widget.clipboard_clear()
        self._widget.clipboard_append(message)


class SearchableTextWindow(Window):
    """ Base class; Window used to display text on a scrollable and searchable text area """

    def __init__(self, viewer, header=None, text=None):

        # Set initial necessary variables and do other required initialization procedures
        super(SearchableTextWindow, self).__init__(viewer)

        # Stores search string in search box
        self._search_text = StringVar()
        self._add_trace(self._search_text, 'w', self._do_search)

        # Stores whether match case box is selected
        self._match_case = BooleanVar()
        self._add_trace(self._match_case, 'w', self._do_search, default=False)

        # Stores a list of 3-valued tuples, each containing the line number, start position
        # and stop position of each result that matches the search string; and stores the index
        # of the tuple for the last shown result
        self._search_match_results = []
        self._search_match_idx = -1

        # Stores header text
        self._header_text = StringVar()

        # Create the menu
        self._add_menu()

        # Create the rest of the SearchableTextWindow content
        self._text_pad = None
        self._search_match_label = None
        self._draw_content()

        # Add notify event for scroll wheel (used to scroll label via scroll wheel without focus)
        self._bind_scroll_event(self._mousewheel_scroll)

        # Set header and window text, if given. We only show window by default if text was given
        if header is not None:
            self._set_header(header)

        if text is not None:
            self._set_text(text)

            self._center_and_fit_to_content()
            self._show_window()

    # Adds menu options used for manipulating the label display
    def _add_menu(self):

        # Create File Menu
        self._menu = Menu(self._widget)
        self._add_file_menu()

        # Create Edit Menu
        edit_menu = Menu(self._menu, tearoff=0)
        edit_menu.add_command(label='Select All', command=self._select_all)
        edit_menu.add_command(label='Copy', command=self._copy)
        self._menu.add_cascade(label='Edit', menu=edit_menu)

    # Draws the majority of the SearchableTextWindow content (header, text pad, scrollbars and search box)
    def _draw_content(self):

        # Add header
        header_box = Frame(self._widget, bg=self.get_bg('gray'))
        header_box.pack(side='top', fill='x')

        header = Label(header_box, textvar=self._header_text, bg=self.get_bg('gray'),
                       font=self.get_font(15, 'bold'))
        header.pack(pady=10, side='top')

        # Search box's parent frame (ensures that search box remains in view when window is resized down)
        search_box_parent_frame = Frame(self._widget, bg=self.get_bg('gray'))
        search_box_parent_frame.pack(side='bottom', fill='x', anchor='nw')

        # Add search box
        search_box_frame = Frame(search_box_parent_frame, height=20, bg=self.get_bg('gray'))
        search_box_frame.pack_propagate(False)

        search_box = Entry(search_box_frame, bg='white', bd=0, highlightthickness=0,
                           textvariable=self._search_text)
        search_box.pack(side='left')
        search_box.bind('<Return>', self._do_search)
        search_box.focus()

        search_button = Button(search_box_frame, text='Search', width=7, command=self._do_search,
                               bg=self.get_bg('gray'))
        search_button.pack(side='left', padx=(5, 0))
        search_box_frame.pack(fill='x', padx=(5, 0), pady=5)

        match_case_button = Checkbutton(search_box_frame, text='Match Case', variable=self._match_case,
                                        bg=self.get_bg('gray'))
        match_case_button.pack(side='left', padx=(5, 0))

        self._search_match_label = Label(search_box_frame, fg='slate gray', bg=self.get_bg('gray'))

        # Add text pad
        text_pad_frame = Frame(self._widget)
        self._create_text_pad(text_pad_frame)

        # Add scrollbars for text pad
        vert_scrollbar = Scrollbar(text_pad_frame, orient='vertical', command=self._text_pad.yview)
        self._text_pad.configure(yscrollcommand=vert_scrollbar.set)

        horz_scrollbar = Scrollbar(text_pad_frame, orient='horizontal', command=self._text_pad.xview)
        self._text_pad.configure(xscrollcommand=horz_scrollbar.set)

        vert_scrollbar.pack(side='right', fill='y')
        horz_scrollbar.pack(side='bottom', fill='x')
        self._text_pad.pack(side='left', expand=1, fill='both')
        text_pad_frame.pack(side='top', expand=1, fill='both')

    # Creates text pad
    def _create_text_pad(self, frame):
        self._text_pad = Text(frame, width=100, height=30, wrap='none', relief='flat',
                              highlightthickness=0, bd=0, bg='white')

    # Sets text shown in text pad
    def _set_text(self, text):

        # Clear text pad of any current content
        self._text_pad.config(state='normal')
        self._text_pad.delete('1.0', 'end')
        self._text_pad.tag_delete(self._text_pad.tag_names())
        self._text_pad.see('0.0')

        # Remove focus from text pad if it has it. Inserting large amount of new text into an existing
        # text pad seems to be extremely slow if it has focus.
        if self._text_pad.focus_get() == self._text_pad:
            self._widget.focus()
            self._widget.update()

        # Insert new content into text pad
        self._text_pad.insert('1.0', text)
        self._text_pad.config(state='disabled')

    # Sets text shown in header
    def _set_header(self, header):
        self._header_text.set(header)

    # Searches for the string in the search box
    def _do_search(self, *args):

        # Get text in search box, and whether match case is selected
        search_text = self._search_text.get().strip('\n\r')
        match_case = self._match_case.get()

        # Remove any previous match, and configure tag for new match
        self._text_pad.tag_delete('search')
        self._text_pad.tag_configure('search', background='yellow')

        # Do not try to search if search string is empty
        if not search_text.strip():
            self._search_match_results = []
            self._update_search_match_label(match_num=0, total_matches=0, action='hide')
            return

        # Start search from beginning unless method was called by an event binding (likely for the enter key)
        # or without arguments (meaning search button was pressed)
        if len(args) != 0 and not isinstance(args[0], TKEvent):

            self._search_match_results = []
            self._search_match_idx = -1

            text_pad_string = self._text_pad.get('1.0', 'end-1c')

            if not match_case:
                text_pad_string = text_pad_string.lower()
                search_text = search_text.lower()

            # Find start and stop positions of each match for search_text. Note that although we could
            # use Text widget's native search, it is extremely slow if the widget has thousands of lines
            # (i.e, for a large label)
            for i, line in enumerate(text_pad_string.splitlines()):

                start_position = 0

                # For each line with at least one match, determine start and stop positions
                # of each match in the line
                for j in range(0, line.count(search_text)):
                    start_position = line.find(search_text, start_position)
                    stop_position = start_position + len(search_text)

                    self._search_match_results.append((i, start_position, stop_position))
                    start_position = stop_position

        # Do not continue if no results found
        num_matches = len(self._search_match_results)

        if num_matches == 0:
            self._update_search_match_label(match_num=0, total_matches=0, action='draw')
            return

        # Find current match
        self._search_match_idx += 1

        if self._search_match_idx >= num_matches:
            self._search_match_idx = 0

        matching_result = self._search_match_results[self._search_match_idx]
        start_index = '{0}.{1}'.format(matching_result[0] + 1, matching_result[1])
        stop_index = '{0}.{1}'.format(matching_result[0] + 1, matching_result[2])

        # Show the match
        self._text_pad.tag_add('search', start_index, stop_index)
        self._text_pad.see(stop_index)

        self._update_search_match_label(match_num=self._search_match_idx, total_matches=num_matches,
                                        action='draw')

    # Updates current display to show whether match was not found for search
    def _update_search_match_label(self, match_num=0, total_matches=0, action='draw'):

        if total_matches > 0:
            matches_text = '({0} of {1} matches)'.format(match_num + 1, total_matches)
        else:
            matches_text = '(No match found)'

        self._search_match_label.config(text=matches_text)

        if action == 'draw':
            self._search_match_label.pack(side='left', padx=(5, 0))

        else:
            self._search_match_label.pack_forget()

    def _select_all(self):
        self._text_pad.focus()
        self._text_pad.tag_add('sel', '1.0', 'end')

    def _copy(self):

        if self._text_pad.tag_ranges('sel'):
            message = self._text_pad.get('sel.first', 'sel.last')
        else:
            message = ''

        self._widget.clipboard_clear()
        self._widget.clipboard_append(message)

    # Called on mouse wheel scroll action, scrolls label up or down
    def _mousewheel_scroll(self, event):

        event_delta = int(-1 * event.delta)

        if platform.system() != 'Darwin':
            event_delta //= 120

        self._text_pad.yview_scroll(event_delta, 'units')


# Sets the icon for a Tkinter widget
def _set_icon(tk_widget, icon_name='logo'):

    # Path for frozen case, e.g. via PyInstaller
    if hasattr(sys, 'frozen'):
        icon_path = os.path.join(sys.prefix, icon_name)

    # Path for unfrozen case
    else:
        icon_path = os.path.join(os.path.dirname(__file__), 'logo', icon_name)

    # Add proper platform-specific icon extensions
    if platform.system() == 'Windows':
        icon_path += '.ico'
    else:
        icon_path += '.gif'

    if os.access(icon_path, os.R_OK):

        try:

            # On Windows, we can use the ico format as a bitmap
            if platform.system() == 'Windows':
                tk_widget.iconbitmap(icon_path)

            # On other platforms we use the gif format since it is guaranteed to work
            else:
                icon = PhotoImage(file=icon_path)
                tk_widget.tk.call('wm', 'iconphoto', tk_widget._w, icon)

        # In rare cases the above call can fail, therefore we do not assign an icon
        except TclError:
            pass


# Execute a function from ``pds4_tools.viewer.mpl``. Ordinarily, we could simply import the relevant
# functions at the very top of this code and call them when necessary. However, the aforementioned
# module will attempt to force-set the TK backend immediately due to MPL requirements to do this prior
# to import from backends. This backend force-set is undesired unless a user is actually attempting
# to use PDS4 Viewer, yet this module and therefore all of its imports are imported by default when a
# user imports anything from the package. Therefore, whenever needed, we use this function to import
# and call the functions we otherwise would have imported at the top.
def _mpl_commands(function_name, *args, **kwargs):

    # Check if MPL is available
    try:
        import matplotlib as mpl
    except ImportError:
        return

    # Safe import of required MPL code
    from . import mpl as mpl_module
    getattr(mpl_module, function_name)(*args, **kwargs)


def pds4_viewer(filename=None, from_existing_structures=None, lazy_load=True, quiet=False):
    """ Displays PDS4 compliant data in a GUI.

    Given a PDS4 label, displays PDS4 data described in the label and
    associated label meta data in a GUI. By default all data structures described
    in the label are read-in and displayed. Can be called without any
    parameters, opening a GUI that has a File->Open function to select
    desired label to be read-in and displayed.

    Parameters:

        filename : str or unicode, optional
            The filename, including full or relative path if necessary, of
            the PDS4 label describing the data to be viewed.
        from_existing_structures : StructureList, optional
            An existing StructureList, as returned by pds4_read(), to view. Takes
            precedence if given together with filename.
        lazy_load : bool, optional
            Do not read-in data of each data structure until attempt to view said
            data structure. Defaults to True.
        quiet : bool, optional
            Suppresses all info/warnings from being output and displayed.
            Defaults to False.
    """

    from .summary_view import open_summary

    # Initialize Viewer cache
    # (This must be done prior to MPL import and should be done as early as possible)
    cache.init_cache()

    # Create viewer, and open summary window
    root = Tk()
    viewer = PDS4Viewer(root)

    open_summary(viewer, filename=filename, from_existing_structures=from_existing_structures,
                 quiet=quiet, lazy_load=lazy_load)

    root.mainloop()


def main():
    """ Wrapper around opening the viewer via a script

    Generally one should use `core.pds4_viewer` instead of this wrapper if using the viewer
    as a module instead of a script. """

    # Safe import of argparse
    try:
        import argparse
    except ImportError:
        from ..extern import argparse

    # Create program arguments
    parser = argparse.ArgumentParser()
    parser.register('type', 'bool', lambda x: x.lower() in ('yes', 'true', 't', '1'))

    parser.add_argument("filename", help="Filename, including full path, of the label", nargs='?', default=None)
    parser.add_argument("--lazy_load", help="Do not read-in data until attempt to view it. Defaults to True",
                        type='bool', default=True)
    parser.add_argument("--quiet", help="Suppresses all info/warnings", type='bool', default=False)

    args = parser.parse_args()

    pds4_viewer(filename=args.filename, lazy_load=args.lazy_load, quiet=args.quiet)


if __name__ == '__main__':
    main()