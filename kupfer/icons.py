import os

import gtk
from gtk import ICON_LOOKUP_USE_BUILTIN, ICON_LOOKUP_FORCE_SIZE
from gtk.gdk import pixbuf_new_from_file_at_size
from gio import Icon, ThemedIcon, FileIcon, File
from gio import FILE_ATTRIBUTE_STANDARD_ICON, FILE_ATTRIBUTE_THUMBNAIL_PATH
from gobject import GError

from kupfer import config
from kupfer import datatools
from kupfer import pretty
from kupfer import scheduler

icon_cache = {}

# number of elements in icon lru cache (per icon size)
ICON_CACHE_SIZE = 15

LARGE_SZ = 128
SMALL_SZ = 24

gtk.icon_size_register("kupfer-large", LARGE_SZ, LARGE_SZ)
gtk.icon_size_register("kupfer-small", SMALL_SZ, SMALL_SZ)

## default fallbacks for our themable icons
kupfer_icon_fallbacks = {
	'kupfer-execute': 'gtk-execute',
	'kupfer-object': 'gtk-file',
	'kupfer-object-multiple': 'gtk-file',
	'kupfer-catalog': 'folder-saved-search',
}

kupfer_locally_installed_names = set()

def _icon_theme_changed(theme):
	pretty.print_info(__name__, "Icon theme changed, clearing cache")
	global icon_cache
	icon_cache = {}

_default_theme = gtk.icon_theme_get_default()
_default_theme.connect("changed", _icon_theme_changed)
_local_theme = gtk.IconTheme()
_local_theme.set_search_path([])

def parse_load_icon_list(icon_list_data, get_data_func, plugin_name=None):
	"""
	@icon_list_data: A bytestring whose lines identify icons
	@get_data_func: A function to return the data for a relative filename
	@plugin_name: plugin id, if applicable
	"""
	for line in icon_list_data.splitlines():
		# ignore '#'-comments
		if line.startswith("#") or not line.strip():
			continue
		fields = map(str.strip, line.split('\t'))
		if len(fields) < 2:
			pretty.print_error(__name__, "Malformed icon-list line %r from %r" %
			                   (line, plugin_name))
			continue
		icon_name, basename = fields[:2]
		override = ('!override' in fields)
		def wrap_get_data():
			return get_data_func(basename)
		load_icon_from_func(plugin_name, icon_name, wrap_get_data, override)

def load_icon_from_func(plugin_name, icon_name, get_data_func, override=False):
	"""
	Load icon from @icon_data into the name @icon_name

	@get_data_func: function to retrieve the data if needed
	@override: override the icon theme
	"""
	if not override and icon_name in kupfer_locally_installed_names:
		pretty.print_debug(__name__, "Skipping existing", icon_name)
		return
	if not override and _default_theme.has_icon(icon_name):
		pretty.print_debug(__name__, "Skipping themed icon", icon_name)
		return
	try:
		icon_data = get_data_func()
	except:
		pretty.print_error(__name__, "Error loading icon %r for %r" %
		                   (icon_name, plugin_name))
		pretty.print_exc(__name__)
		return
	for size in (SMALL_SZ, LARGE_SZ):
		pixbuf = get_pixbuf_from_data(icon_data, size, size)
		gtk.icon_theme_add_builtin_icon(icon_name, size, pixbuf)
		pretty.print_debug(__name__, "Loading icon", icon_name, "at", size,
				"for", plugin_name)
	kupfer_locally_installed_names.add(icon_name)

def get_icon(key, icon_size):
	"""
	try retrieve icon in cache
	is a generator so it can be concisely called with a for loop
	"""
	try:
		rec = icon_cache[icon_size][key]
	except KeyError:
		return
	yield rec

def store_icon(key, icon_size, icon):
	"""
	Store an icon in cache. It must not have been stored before
	"""
	assert icon, "icon %s may not be %s" % (key, icon)
	icon_rec = icon
	if icon_size not in icon_cache:
		icon_cache[icon_size] = datatools.LruCache(ICON_CACHE_SIZE)
	icon_cache[icon_size][key] = icon_rec

def _get_icon_dwim(icon, icon_size):
	"""Make an icon at @icon_size where
	@icon can be either an icon name, or a gicon
	"""
	if isinstance(icon, Icon):
		return get_icon_for_gicon(icon, icon_size)
	elif icon:
		return get_icon_for_name(icon, icon_size)
	return None

class ComposedIcon (Icon):
	"""
	A composed icon, which kupfer will render to pixbuf as
	background icon with the decorating icon as emblem
	"""

	class Implementation (object):
		"""Base class for the internal implementation

		@minimum_icon_size is the minimum size for
		the composition to be drawn"""
		minimum_icon_size = 48

		def __init__(self, baseicon, emblem):
			self.baseicon = baseicon
			self.emblemicon = emblem

	class _ThemedIcon (Implementation, ThemedIcon):
		def __init__(self, fallback, baseicon, emblem):
			ComposedIcon.Implementation.__init__(self, baseicon, emblem)
			if isinstance(fallback, basestring):
				names = (fallback, )
			else:
				names = fallback.get_names()
			ThemedIcon.__init__(self, names)

	class _FileIcon (Implementation, FileIcon):
		def __init__(self, fallback, baseicon, emblem):
			ComposedIcon.Implementation.__init__(self, baseicon, emblem)
			FileIcon.__init__(self, fallback.get_file())

	def __new__(cls, baseicon, emblem, emblem_is_fallback=False):
		"""Contstuct a composed icon from @baseicon and @emblem,
		which may be GIcons or icon names (strings)
		"""
		fallback = emblem if emblem_is_fallback else baseicon
		if isinstance(fallback, (basestring, ThemedIcon)):
			return cls._ThemedIcon(fallback, baseicon, emblem)
		if isinstance(fallback, FileIcon):
			return cls._FileIcon(fallback, baseicon, emblem)
		return None


def ComposedIconSmall(baseicon, emblem, **kwargs):
	"""Create composed icon for leaves with emblem visible on browser list"""
	ci = ComposedIcon(baseicon, emblem, **kwargs)
	ci.minimum_icon_size = SMALL_SZ
	return ci


def _render_composed_icon(composed_icon, icon_size):
	# If it's too small, render as fallback icon
	if icon_size < composed_icon.minimum_icon_size:
		return _get_icon_for_standard_gicon(composed_icon, icon_size)
	emblemicon = composed_icon.emblemicon
	baseicon = composed_icon.baseicon
	toppbuf = _get_icon_dwim(emblemicon, icon_size)
	bottompbuf = _get_icon_dwim(baseicon, icon_size)
	if not toppbuf or not bottompbuf:
		return _get_icon_for_standard_gicon(composed_icon, icon_size)

	dest = bottompbuf.copy()
	# @fr is the scale
	fr = 0.6
	dcoord = int((1-fr)*icon_size)
	dsize = int(fr*icon_size)
	# http://library.gnome.org/devel/gdk-pixbuf/unstable//gdk-pixbuf-scaling.html
	toppbuf.composite(dest, dcoord, dcoord, dsize, dsize,
			dcoord, dcoord, fr, fr, gtk.gdk.INTERP_BILINEAR, 255)
	return dest

def get_thumbnail_for_file(uri, width=-1, height=-1):
	"""
	Return a Pixbuf thumbnail for the file at
	the @uri, which can be *either* and uri or a path
	size is @width x @height

	return None if not found
	"""

	gfile = File(uri)
	if not gfile.query_exists():
		return None
	finfo = gfile.query_info(FILE_ATTRIBUTE_THUMBNAIL_PATH)
	thumb_path = finfo.get_attribute_byte_string(FILE_ATTRIBUTE_THUMBNAIL_PATH)

	return get_pixbuf_from_file(thumb_path, width, height)

def get_pixbuf_from_file(thumb_path, width=-1, height=-1):
	"""
	Return a Pixbuf thumbnail for the file at @thumb_path
	sized @width x @height
	For non-icon pixbufs:
	We might cache these, but on different terms than the icon cache
	if @thumb_path is None, return None
	"""
	if not thumb_path:
		return None
	try:
		icon = pixbuf_new_from_file_at_size(thumb_path, width, height)
		return icon
	except GError, e:
		# this error is not important, the program continues on fine,
		# so we put it in debug output.
		pretty.print_debug(__name__, "get_pixbuf_from_file file:", thumb_path,
			"error:", e)

def get_gicon_for_file(uri):
	"""
	Return a GIcon representing the file at
	the @uri, which can be *either* and uri or a path

	return None if not found
	"""

	gfile = File(uri)
	if not gfile.query_exists():
		return None

	finfo = gfile.query_info(FILE_ATTRIBUTE_STANDARD_ICON)
	gicon = finfo.get_attribute_object(FILE_ATTRIBUTE_STANDARD_ICON)
	# very manually override generic folder icon name
	if isinstance(gicon, ThemedIcon):
		if gicon.get_names()[0] == "inode-directory":
			return ThemedIcon("folder")
	return gicon

def get_icon_for_gicon(gicon, icon_size):
	"""
	Return a pixbuf of @icon_size for the @gicon

	NOTE: Currently only the following can be rendered:
		gio.ThemedIcon
		gio.FileIcon
		kupfer.icons.ComposedIcon
	"""
	# FIXME: We can't load any general GIcon
	if not gicon:
		return None
	if isinstance(gicon, ComposedIcon.Implementation):
		return _render_composed_icon(gicon, icon_size)
	return _get_icon_for_standard_gicon(gicon, icon_size)

def _get_icon_for_standard_gicon(gicon, icon_size):
	"""Render ThemedIcon and FileIcon"""
	if isinstance(gicon, FileIcon):
		ifile = gicon.get_file()
		return get_icon_from_file(ifile.get_path(), icon_size)
	if isinstance(gicon, ThemedIcon):
		names = gicon.get_names()
		return get_icon_for_name(names[0], icon_size, names)
	print "get_icon_for_gicon, could not load", gicon
	return None


def _setup_icon_renderer(sched):
	from kupfer.core import settings
	setctl = settings.GetSettingsController()
	setctl.connect("alternatives-changed::icon_renderer", _icon_render_change)
	setctl.connect("value-changed::tools.icon_renderer", _icon_render_change)
	_icon_render_change(setctl)

def _icon_render_change(setctl, *arguments):
	global _IconRenderer
	renderer_dict = setctl.get_preferred_alternative('icon_renderer')
	renderer = renderer_dict.get("renderer")
	if not renderer or renderer is _IconRenderer:
		return
	pretty.print_debug(__name__, "Using", renderer)
	_icon_theme_changed(None)
	_IconRenderer = renderer

scheduler.GetScheduler().connect("loaded", _setup_icon_renderer)


class IconRenderer (object):
	"""
	Default GTK+ implementation
	"""
	@classmethod
	def pixbuf_for_name(cls, icon_name, icon_size):
		if icon_name in kupfer_locally_installed_names:
			try:
				return _local_theme.load_icon(icon_name, icon_size,
				                              ICON_LOOKUP_USE_BUILTIN |
				                              ICON_LOOKUP_FORCE_SIZE)
			except GError:
				pass
		try:
			return _default_theme.load_icon(icon_name, icon_size,
			                                ICON_LOOKUP_USE_BUILTIN |
			                                ICON_LOOKUP_FORCE_SIZE)
		except GError:
			pass

	@classmethod
	def pixbuf_for_file(cls, file_path, icon_size):
		try:
			icon = gtk.gdk.pixbuf_new_from_file_at_size(file_path, icon_size,
			                                            icon_size)
			return icon
		except GError:
			pretty.print_exc(__name__)

_IconRenderer = IconRenderer


def get_icon_for_name(icon_name, icon_size, icon_names=[]):
	for i in get_icon(icon_name, icon_size):
		return i
	if not icon_names: icon_names = (icon_name,)

	# Try the whole list of given names
	for load_name in icon_names:
		try:
			icon = _IconRenderer.pixbuf_for_name(load_name, icon_size)
			if icon:
				break
			elif icon_name in kupfer_icon_fallbacks:
				fallback_name = kupfer_icon_fallbacks[icon_name]
				icon = _IconRenderer.pixbuf_for_name(fallback_name, icon_size)
				if icon:
					break
		except Exception:
			pretty.print_exc(__name__)
			icon = None
	else:
		# if we did not reach 'break' in the loop
		return None
	# We store the first icon in the list, even if the match
	# found was later in the chain
	store_icon(icon_name, icon_size, icon)
	return icon

def get_icon_from_file(icon_file, icon_size):
	# try to load from cache
	for icon in get_icon(icon_file, icon_size):
		return icon
	icon = _IconRenderer.pixbuf_for_file(icon_file, icon_size)
	if icon is not None:
		store_icon(icon_file, icon_size, icon)
		return icon

def is_good(gicon):
	"""Return True if it is likely that @gicon will load a visible icon
	(icon name exists in theme, or icon references existing file)
	"""
	if not gicon:
		return False
	if isinstance(gicon, ThemedIcon):
		return bool(get_good_name_for_icon_names(gicon.get_names()))
	if isinstance(gicon, FileIcon):
		ifile = gicon.get_file()
		return ifile.query_exists()
	# Since we can't load it otherwise (right now, see above)
	return False

def get_gicon_with_fallbacks(gicon, names):
	if not is_good(gicon):
		for name in names:
			gicon = ThemedIcon(name)
			if is_good(gicon):
				return gicon
		return ThemedIcon(name)
	return gicon

def get_good_name_for_icon_names(names):
	"""Return first name in @names that exists
	in current icon theme, or None
	"""
	for name in names:
		if _default_theme.has_icon(name):
			return name
	return None

def get_gicon_for_names(*names):
	return ThemedIcon(names)


def get_pixbuf_from_data(data, width=None, height=None):
	"""Create pixbuf object from data with optional scaling

	@data: picture as raw data
	@width, @heigh: optional destination size
	"""
	def set_size(img, img_width, img_height):
		scale = min(width/float(img_width), height/float(img_height))
		new_width, new_height = int(img_width*scale), int(img_height*scale)
		img.set_size(new_width, new_height)

	ploader = gtk.gdk.PixbufLoader()
	if width and height:
		ploader.connect("size-prepared", set_size)
	ploader.write(data)
	ploader.close()
	return ploader.get_pixbuf()

