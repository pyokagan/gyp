#!/usr/bin/env python

# Copyright (c) 2012 Google Inc. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import copy
import gyp.input
import optparse
import os.path
import re
import shlex
import sys
import traceback
from gyp.common import GypError
import subprocess
import glob
import platform
import collections

# Path to package directory
_pkg_path = os.path.dirname(__file__)

# Default debug modes for GYP
debug = {}

# List of "official" debug modes, but you can use anything you like.
DEBUG_GENERAL = 'general'
DEBUG_VARIABLES = 'variables'
DEBUG_INCLUDES = 'includes'


def DebugOutput(mode, message, *args):
  if 'all' in gyp.debug or mode in gyp.debug:
    ctx = ('unknown', 0, 'unknown')
    try:
      f = traceback.extract_stack(limit=2)
      if f:
        ctx = f[0][:3]
    except:
      pass
    if args:
      message %= args
    print '%s:%s:%d:%s %s' % (mode.upper(), os.path.basename(ctx[0]),
                              ctx[1], ctx[2], message)

def FindBuildFiles():
  extension = '.gyp'
  files = os.listdir(os.getcwd())
  build_files = []
  for file in files:
    if file.endswith(extension):
      build_files.append(file)
  return build_files


def Load(build_files, format, default_variables={},
         includes=[], depth='.', params=None, check=False,
         circular_check=True):
  """
  Loads one or more specified build files.
  default_variables and includes will be copied before use.
  Returns the generator for the specified format and the
  data returned by loading the specified build files.
  """
  if params is None:
    params = {}

  flavor = None
  if '-' in format:
    format, params['flavor'] = format.split('-', 1)

  default_variables = copy.copy(default_variables)

  # Default variables provided by this program and its modules should be
  # named WITH_CAPITAL_LETTERS to provide a distinct "best practice" namespace,
  # avoiding collisions with user and automatic variables.
  default_variables['GENERATOR'] = format

  # Format can be a custom python file, or by default the name of a module
  # within gyp.generator.
  if format.endswith('.py'):
    generator_name = os.path.splitext(format)[0]
    path, generator_name = os.path.split(generator_name)

    # Make sure the path to the custom generator is in sys.path
    # Don't worry about removing it once we are done.  Keeping the path
    # to each generator that is used in sys.path is likely harmless and
    # arguably a good idea.
    path = os.path.abspath(path)
    if path not in sys.path:
      sys.path.insert(0, path)
  else:
    generator_name = 'gyp.generator.' + format

  # These parameters are passed in order (as opposed to by key)
  # because ActivePython cannot handle key parameters to __import__.
  generator = __import__(generator_name, globals(), locals(), generator_name)
  for (key, val) in generator.generator_default_variables.items():
    default_variables.setdefault(key, val)

  # Give the generator the opportunity to set additional variables based on
  # the params it will receive in the output phase.
  if getattr(generator, 'CalculateVariables', None):
    generator.CalculateVariables(default_variables, params)

  # Give the generator the opportunity to set generator_input_info based on
  # the params it will receive in the output phase.
  if getattr(generator, 'CalculateGeneratorInputInfo', None):
    generator.CalculateGeneratorInputInfo(params)

  # Fetch the generator specific info that gets fed to input, we use getattr
  # so we can default things and the generators only have to provide what
  # they need.
  generator_input_info = {
    'non_configuration_keys':
        getattr(generator, 'generator_additional_non_configuration_keys', []),
    'path_sections':
        getattr(generator, 'generator_additional_path_sections', []),
    'extra_sources_for_rules':
        getattr(generator, 'generator_extra_sources_for_rules', []),
    'generator_supports_multiple_toolsets':
        getattr(generator, 'generator_supports_multiple_toolsets', False),
    'generator_wants_static_library_dependencies_adjusted':
        getattr(generator,
                'generator_wants_static_library_dependencies_adjusted', True),
    'generator_wants_sorted_dependencies':
        getattr(generator, 'generator_wants_sorted_dependencies', False),
    'generator_filelist_paths':
        getattr(generator, 'generator_filelist_paths', None),
  }

  # Process the input specific to this generator.
  result = gyp.input.Load(build_files, default_variables, includes[:],
                          depth, generator_input_info, check, circular_check,
                          params['parallel'], params['root_targets'])
  return [generator] + result

def NameValueListToDict(name_value_list):
  """
  Takes an array of strings of the form 'NAME=VALUE' and creates a dictionary
  of the pairs.  If a string is simply NAME, then the value in the dictionary
  is set to True.  If VALUE can be converted to an integer, it is.
  """
  result = { }
  for item in name_value_list:
    tokens = item.split('=', 1)
    if len(tokens) == 2:
      # If we can make it an int, use that, otherwise, use the string.
      try:
        token_value = int(tokens[1])
      except ValueError:
        token_value = tokens[1]
      # Set the variable to the supplied value.
      result[tokens[0]] = token_value
    else:
      # No value supplied, treat it as a boolean and set it.
      result[tokens[0]] = True
  return result

def ShlexEnv(env_name):
  flags = os.environ.get(env_name, [])
  if flags:
    flags = shlex.split(flags)
  return flags

def FormatOpt(opt, value):
  if opt.startswith('--'):
    return '%s=%s' % (opt, value)
  return opt + value

class Triple(collections.namedtuple('Triple', ('arch', 'os', 'env'))):
  def __str__(self):
    return '{}-{}-{}'.format(*self)

  @classmethod
  def parse(cls, target):
    x = target.split('-', 2)
    return cls(*x)

  def gnu(self):
    """GNU normalization"""
    arch, os, env = self
    # Normalize i*86 -> x86
    if re.match(r'^i\d86$', arch):
      arch = 'x86'
    if arch == 'x64':
      arch = 'x86_64'
    return self.__class__(arch, os, env)

  def gyp(self):
    """GYP normalization"""
    arch, os, env = self
    if re.match(r'^i\d86$', arch):
      arch = 'x86'
    if arch == 'x86_64':
      arch = 'x64'
    return self.__class__(arch, os, env)


class Version(tuple):
  def __new__(cls, *args):
    return tuple.__new__(cls, tuple(args))

  def __getnewargs__(self):
    return tuple(self)

  def __str__(self):
    return ".".join([str(x) for x in self])


def find_executables(names, paths=None):
  """Given a list of executable names, find them and return their
  absolute paths."""
  if paths is None:
    pathlist = os.getenv('PATH', '').split(os.pathsep)
  else:
    pathlist = paths
  if sys.platform.startswith('win'):
    pathlist.insert(0, os.curdir)
    exts = os.getenv('PATHEXT', '').split(os.pathsep)
  else:
    exts = ['']
  for name in names:
    for prefix in pathlist:
      for suffix in exts:
        path = os.path.join(prefix, name) + suffix
        if os.access(path, os.F_OK | os.X_OK):
          yield path


def find_cc(host=None, paths=None):
  """Finds a list of possible C compilers.
  If host is provided, we will also try to prefix the host target in front
  of the compiler."""
  if 'CC' in os.environ:
    return iter((os.environ['CC'],))
  x = ['gcc', 'gcc-4.8', 'gcc-4.7', 'gcc-4.6']
  if host:
    y = ['{}-{}'.format(host, z) for z in x]
  else:
    y = []
  return find_executables(y + x, paths)


def identify_cc(path):
  """Given a C compiler, identify it.
  Returns (type, version, target) where version is a version tuple
  Returns one of ("gcc", "msvc", "unknown")
  """
  #To detect MSVC, run command with no arguments
  p = subprocess.Popen((path,), stdout=subprocess.PIPE, stdin=subprocess.PIPE,
      stderr=subprocess.PIPE)
  o = p.stderr.read()
  p.wait()
  m = re.search((r'^Microsoft \(R\) C/C\+\+ Optimizing Compiler Version '
    '([\d\.]+) for ([A-Za-z0-9\-_]+)'), o, re.MULTILINE)
  if m:
    version = Version(*[int(x) for x in m.group(1).split('.')])
    arch = m.group(2)
    os = "win"
    env = "msvc"
    return ("msvc", version, Triple(arch, os, env))
  # Detect "Microsoft (R) C/C++ Optimizing Compiler Version X" in stderr
  # If this fails, try running CC -v
  p = subprocess.Popen((path, '-v'), stdout=subprocess.PIPE,
      stdin=subprocess.PIPE, stderr=subprocess.PIPE)
  _, o = p.communicate()
  # Look for gcc version X in stdout
  m = re.search(r'^gcc version ([\d\.]+)', o, re.MULTILINE)
  m2 = re.search(r'^Target: ([A-Za-z0-9\-_]+)$', o, re.MULTILINE)
  if m and m2:
    version = Version(*[int(x) for x in m.group(1).split(".")])
    return ('gcc', version, Triple.parse(m2.group(1)))
  # Or else return unknown
  return ('unknown', (), Triple('unknown', 'unknown', 'unknown'))


def find_cxx(host=None, paths=None):
  """Finds a list of possible C++ compilers.
  If host is provided, we will also try to prefix the host target in front
  of the compiler."""
  if 'CXX' in os.environ:
    return iter((os.environ['CXX'],))
  x = ['g++', 'g++-4.8', 'g++-4.7', 'g++-4.6']
  if host:
    y = ['{}-{}'.format(host, z) for z in x]
  else:
    y = []
  return find_executables(y + x, paths)


def identify_cxx(path):
  """Given a C++ compiler, identify it.
  Returns (type, version, target) where version is a version tuple
  `type` is one of ("gcc", "msvc", "unknown")
  """
  return identify_cc(path)


def detect_build():
  """Detect build system triple"""
  arch = platform.machine()
  if sys.platform.startswith('win'):
    os = 'win'
    env = "msvc"
  elif sys.platform.startswith('linux'):
    os = 'linux'
    env = 'gnu' #TODO: Always GNU?
  elif sys.platform.startswith('cygwin'):
    os = 'win'
    env = 'cygwin'
  else:
    os = 'unknown'
    env = 'unknown'
  return Triple(arch, os, env)


def find_ar(host=None, paths=None):
  """Finds a list of possible ar (Archive editor)
  If host is provided, we will also try to prefix the host target
  in front of the compiler."""
  if 'AR' in os.environ:
    return iter((os.environ['AR'],))
  x = ['ar', 'gcc-ar-4.8', 'gcc-ar-4.7']
  y = ['{}-{}'.format(host, z) for z in x] if host else []
  return find_executables(y + x, paths)


def find_link(host=None, paths=None):
  if 'LINK' in os.environ:
    return iter((os.environ['LINK'],))
  x = ['g++', 'g++-4.8', 'g++-4.7', 'g++-4.6']
  y = ['{}-{}'.format(host, z) for z in x] if host else []
  return find_executables(y + x, paths)


def find_make(host=None, paths=None):
  "Finds a list of possible build systems"
  #TODO: Mac support, xcode??
  if sys.platform.startswith('win'):
    #Only msbuild is supported on Windows. Makefiles will very
    #easily break on Windows
    return find_executables(['msbuild'], paths)
  # Prefer ninja, then GNU Make
  return find_executables(['ninja', 'gmake', 'make'], paths)


def identify_make(path):
  """Given a build system executable, identify it.
  Returns (type, version), where `type` is one of 
  {gmake, msbuild, ninja, unknown}, and `version` is a Version tuple.
  """
  # Run BUILD /version to identify msbuild
  # Run BUILD --version  to identify gmake
  p = subprocess.Popen((path, '--version'), stdout=subprocess.PIPE,
      stdin=subprocess.PIPE, stderr=subprocess.PIPE)
  stdout, _ = p.communicate()
  m = re.match(r'GNU Make ([\d\.]+)', stdout)
  if m:
    version = Version(*[int(x) for x in m.group(1).split('.')])
    return ('gmake', version)
  # Else, run BUILD --help and look for usage: ninja to identify ninja
  p = subprocess.Popen((path, '--help'), stdout=subprocess.PIPE,
      stdin=subprocess.PIPE, stderr=subprocess.PIPE)
  _, stderr = p.communicate()
  m = re.match(r'usage: ninja', stderr)
  if m:
    version = Version(*[int(x) for x in stdout.strip().split('.')])
    return ('ninja', version)
  # Or else return unknown
  return ('unknown', ())


def find_pkg_config(host=None, paths=None):
  """Finds a list of possible pkg-config.
  If host is provided, we will also try to prefix the host target in front
  of the compiler."""
  if sys.platform.startswith('win'):
    return []
  x = ['pkg-config']
  if host:
    y = ['{}-{}'.format(host, z) for z in x]
  else:
    y = []
  return find_executables(y + x, paths)


def android_get_hosts(path, build):
  """Get possible toolchains"""
  paths = glob.glob('{}/toolchains/*/prebuilt/{}-{}/bin/*-gcc'.format(path, build[1], build[0]))
  hosts = (Triple.parse(os.path.basename(x)[:-4]) for x in paths)
  return set(hosts)


def RegenerateAppendFlag(flag, values, predicate, env_name, options):
  """Regenerate a list of command line flags, for an option of action='append'.

  The |env_name|, if given, is checked in the environment and used to generate
  an initial list of options, then the options that were specified on the
  command line (given in |values|) are appended.  This matches the handling of
  environment variables and command line flags where command line flags override
  the environment, while not requiring the environment to be set when the flags
  are used again.
  """
  flags = []
  if options.use_environment and env_name:
    for flag_value in ShlexEnv(env_name):
      value = FormatOpt(flag, predicate(flag_value))
      if value in flags:
        flags.remove(value)
      flags.append(value)
  if values:
    for flag_value in values:
      flags.append(FormatOpt(flag, predicate(flag_value)))
  return flags

def RegenerateFlags(options):
  """Given a parsed options object, and taking the environment variables into
  account, returns a list of flags that should regenerate an equivalent options
  object (even in the absence of the environment variables.)

  Any path options will be normalized relative to depth.

  The format flag is not included, as it is assumed the calling generator will
  set that as appropriate.
  """
  def FixPath(path):
    path = gyp.common.FixIfRelativePath(path, options.depth)
    if not path:
      return os.path.curdir
    return path

  def Noop(value):
    return value

  # We always want to ignore the environment when regenerating, to avoid
  # duplicate or changed flags in the environment at the time of regeneration.
  flags = ['--ignore-environment']
  for name, metadata in options._regeneration_metadata.iteritems():
    opt = metadata['opt']
    value = getattr(options, name)
    value_predicate = metadata['type'] == 'path' and FixPath or Noop
    action = metadata['action']
    env_name = metadata['env_name']
    if action == 'append':
      flags.extend(RegenerateAppendFlag(opt, value, value_predicate,
                                        env_name, options))
    elif action in ('store', None):  # None is a synonym for 'store'.
      if value:
        flags.append(FormatOpt(opt, value_predicate(value)))
      elif options.use_environment and env_name and os.environ.get(env_name):
        flags.append(FormatOpt(opt, value_predicate(os.environ.get(env_name))))
    elif action in ('store_true', 'store_false'):
      if ((action == 'store_true' and value) or
          (action == 'store_false' and not value)):
        flags.append(opt)
      elif options.use_environment and env_name:
        print >>sys.stderr, ('Warning: environment regeneration unimplemented '
                             'for %s flag %r env_name %r' % (action, opt,
                                                             env_name))
    else:
      print >>sys.stderr, ('Warning: regeneration unimplemented for action %r '
                           'flag %r' % (action, opt))

  return flags

class RegeneratableOptionParser(optparse.OptionParser):
  def __init__(self):
    self.__regeneratable_options = {}
    optparse.OptionParser.__init__(self)

  def add_option(self, *args, **kw):
    """Add an option to the parser.

    This accepts the same arguments as OptionParser.add_option, plus the
    following:
      regenerate: can be set to False to prevent this option from being included
                  in regeneration.
      env_name: name of environment variable that additional values for this
                option come from.
      type: adds type='path', to tell the regenerator that the values of
            this option need to be made relative to options.depth
    """
    env_name = kw.pop('env_name', None)
    if 'dest' in kw and kw.pop('regenerate', True):
      dest = kw['dest']

      # The path type is needed for regenerating, for optparse we can just treat
      # it as a string.
      type = kw.get('type')
      if type == 'path':
        kw['type'] = 'string'

      self.__regeneratable_options[dest] = {
          'action': kw.get('action'),
          'type': type,
          'env_name': env_name,
          'opt': args[0],
        }

    optparse.OptionParser.add_option(self, *args, **kw)

  def parse_args(self, *args):
    values, args = optparse.OptionParser.parse_args(self, *args)
    values._regeneration_metadata = self.__regeneratable_options
    return values, args

def gyp_main(args):
  my_name = os.path.basename(sys.argv[0])

  parser = RegeneratableOptionParser()
  usage = 'usage: %s [options ...] [build_file ...]'
  parser.set_usage(usage.replace('%s', '%prog'))
  parser.add_option('--build', dest='configs', action='append',
                    help='configuration for build after project generation')
  parser.add_option('--check', dest='check', action='store_true',
                    help='check format of gyp files')
  parser.add_option('--config-dir', dest='config_dir', action='store',
                    env_name='GYP_CONFIG_DIR', default=None,
                    help='The location for configuration files like '
                    'include.gypi.')
  parser.add_option('-d', '--debug', dest='debug', metavar='DEBUGMODE',
                    action='append', default=[], help='turn on a debugging '
                    'mode for debugging GYP.  Supported modes are "variables", '
                    '"includes" and "general" or "all" for all of them.')
  parser.add_option('-D', dest='defines', action='append', metavar='VAR=VAL',
                    env_name='GYP_DEFINES',
                    help='sets variable VAR to value VAL')
  parser.add_option('--depth', dest='depth', metavar='PATH', type='path',
                    help='set DEPTH gyp variable to a relative path to PATH')
  parser.add_option('-f', '--format', dest='formats', action='append',
                    env_name='GYP_GENERATORS', regenerate=False,
                    help='output formats to generate')
  parser.add_option('-G', dest='generator_flags', action='append', default=[],
                    metavar='FLAG=VAL', env_name='GYP_GENERATOR_FLAGS',
                    help='sets generator flag FLAG to VAL')
  parser.add_option('--generator-output', dest='generator_output',
                    action='store', default=None, metavar='DIR', type='path',
                    env_name='GYP_GENERATOR_OUTPUT',
                    help='puts generated build files under DIR')
  parser.add_option('--ignore-environment', dest='use_environment',
                    action='store_false', default=True, regenerate=False,
                    help='do not read options from environment variables')
  parser.add_option('-I', '--include', dest='includes', action='append',
                    metavar='INCLUDE', type='path',
                    help='files to include in all loaded .gyp files')
  # --no-circular-check disables the check for circular relationships between
  # .gyp files.  These relationships should not exist, but they've only been
  # observed to be harmful with the Xcode generator.  Chromium's .gyp files
  # currently have some circular relationships on non-Mac platforms, so this
  # option allows the strict behavior to be used on Macs and the lenient
  # behavior to be used elsewhere.
  # TODO(mark): Remove this option when http://crbug.com/35878 is fixed.
  parser.add_option('--no-circular-check', dest='circular_check',
                    action='store_false', default=True, regenerate=False,
                    help="don't check for circular relationships between files")
  parser.add_option('--no-parallel', action='store_true', default=False,
                    help='Disable multiprocessing')
  parser.add_option('-S', '--suffix', dest='suffix', default='',
                    help='suffix to add to generated files')
  parser.add_option('--toplevel-dir', dest='toplevel_dir', action='store',
                    default=None, metavar='DIR', type='path',
                    help='directory to use as the root of the source tree')
  parser.add_option('-R', '--root-target', dest='root_targets',
                    action='append', metavar='TARGET',
                    help='include only TARGET and its deep dependencies')
  parser.add_option('--host', dest='host', action='store',
                    help='cross-compile to build programs to run on HOST')
  parser.add_option('--android', dest='android', action='store_true',
                    help='use android NDK')

  options, build_files_arg = parser.parse_args(args)
  build_files = build_files_arg

  # Set up the configuration directory (defaults to ~/.gyp)
  if not options.config_dir:
    home = None
    home_dot_gyp = None
    if options.use_environment:
      home_dot_gyp = os.environ.get('GYP_CONFIG_DIR', None)
      if home_dot_gyp:
        home_dot_gyp = os.path.expanduser(home_dot_gyp)

    if not home_dot_gyp:
      home_vars = ['HOME']
      if sys.platform in ('cygwin', 'win32'):
        home_vars.append('USERPROFILE')
      for home_var in home_vars:
        home = os.getenv(home_var)
        if home != None:
          home_dot_gyp = os.path.join(home, '.gyp')
          if not os.path.exists(home_dot_gyp):
            home_dot_gyp = None
          else:
            break
  else:
    home_dot_gyp = os.path.expanduser(options.config_dir)

  if home_dot_gyp and not os.path.exists(home_dot_gyp):
    home_dot_gyp = None

  if not options.formats:
    # If no format was given on the command line, then check the env variable.
    generate_formats = []
    if options.use_environment:
      generate_formats = os.environ.get('GYP_GENERATORS', [])
    if generate_formats:
      generate_formats = re.split('[\s,]', generate_formats)
    if generate_formats:
      options.formats = generate_formats
    else:
      # Nothing in the variable, default based on platform.
      if sys.platform == 'darwin':
        options.formats = ['xcode']
      elif sys.platform in ('win32', 'cygwin'):
        options.formats = ['msvs']
      else:
        options.formats = ['make']

  if not options.generator_output and options.use_environment:
    g_o = os.environ.get('GYP_GENERATOR_OUTPUT')
    if g_o:
      options.generator_output = g_o

  options.parallel = not options.no_parallel

  for mode in options.debug:
    gyp.debug[mode] = 1

  # Do an extra check to avoid work when we're not debugging.
  if DEBUG_GENERAL in gyp.debug:
    DebugOutput(DEBUG_GENERAL, 'running with these options:')
    for option, value in sorted(options.__dict__.items()):
      if option[0] == '_':
        continue
      if isinstance(value, basestring):
        DebugOutput(DEBUG_GENERAL, "  %s: '%s'", option, value)
      else:
        DebugOutput(DEBUG_GENERAL, "  %s: %s", option, value)

  if not build_files:
    build_files = FindBuildFiles()
  if not build_files:
    raise GypError((usage + '\n\n%s: error: no build_file') %
                   (my_name, my_name))

  # TODO(mark): Chromium-specific hack!
  # For Chromium, the gyp "depth" variable should always be a relative path
  # to Chromium's top-level "src" directory.  If no depth variable was set
  # on the command line, try to find a "src" directory by looking at the
  # absolute path to each build file's directory.  The first "src" component
  # found will be treated as though it were the path used for --depth.
  if not options.depth:
    for build_file in build_files:
      build_file_dir = os.path.abspath(os.path.dirname(build_file))
      build_file_dir_components = build_file_dir.split(os.path.sep)
      components_len = len(build_file_dir_components)
      for index in xrange(components_len - 1, -1, -1):
        if build_file_dir_components[index] == 'src':
          options.depth = os.path.sep.join(build_file_dir_components)
          break
        del build_file_dir_components[index]

      # If the inner loop found something, break without advancing to another
      # build file.
      if options.depth:
        break

    if not options.depth:
      raise GypError('Could not automatically locate src directory.  This is'
                     'a temporary Chromium feature that will be removed.  Use'
                     '--depth as a workaround.')

  # If toplevel-dir is not set, we assume that depth is the root of our source
  # tree.
  if not options.toplevel_dir:
    options.toplevel_dir = options.depth

  # Detect the current build system
  build = detect_build()
  build_gyp = build.gyp()
  # Handle --host
  if options.host:
    host = options.host = Triple.parse(options.host)
  else:
    host = options.host = build
  # GYP-normalize host
  host_gyp = host.gyp()
  # If --android, detect android ndk path
  android_ndk = os.environ.get('ANDROID_NDK_ROOT')
  if not android_ndk:
    raise GypError('ANDROID_NDK_ROOT environment variable is not defined')
  # Executable search path
  if options.android:
    paths = glob.glob(os.path.join(android_ndk, 'toolchains', '*', 'prebuilt',
      '{}-{}'.format(build.os, build.arch), 'bin'))
  else:
    paths = os.environ.get('PATH', '').split(os.pathsep)
  # Detect the required tools
  cc_path = next(find_cc(host, paths), None)
  cxx_path = next(find_cxx(host, paths), None)
  ar_path = next(find_ar(host, paths), None)
  link_path = next(find_link(host, paths), None)
  if cc_path:
    os.environ['CC'] = cc_path
  if cxx_path:
    os.environ['CXX'] = cxx_path
  if ar_path:
    os.environ['AR'] = ar_path
  if link_path:
    os.environ['LINK'] = link_path
  # -D on the command line sets variable defaults - D isn't just for define,
  # it's for default.  Perhaps there should be a way to force (-F?) a
  # variable's value so that it can't be overridden by anything else.
  cmdline_default_variables = {}
  defines = []
  # Define some host-specific defines that are commonly used in gyp files
  # in the wild. (e.g. v8, nodejs)
  defines.append('OS={}'.format(host_gyp.os))
  defines.append('target_arch={}'.format(host_gyp.arch))
  defines.append('arm_version=7')
  defines.append('host_arch={}'.format(build_gyp.arch))
  if options.android:
    defines.append('android_target_arch={}'.format(host_gyp.arch))
  if host_gyp.arch == "arm":
    # TODO: Detect these variables instead!
    defines.append('armv7=1')
    defines.append('arm_fpu=vfpv3')
    defines.append('arm_neon=0')
    defines.append('arm_thumb=0')
    defines.append('arm_float_abi=default')

  if options.use_environment:
    defines += ShlexEnv('GYP_DEFINES')
  if options.defines:
    defines += options.defines
  cmdline_default_variables = NameValueListToDict(defines)
  if DEBUG_GENERAL in gyp.debug:
    DebugOutput(DEBUG_GENERAL,
                "cmdline_default_variables: %s", cmdline_default_variables)

  # Set up includes.
  includes = []
  if options.android:
    includes.append(os.path.join(_pkg_path, 'android.gypi'))

  # If ~/.gyp/include.gypi exists, it'll be forcibly included into every
  # .gyp file that's loaded, before anything else is included.
  if home_dot_gyp != None:
    default_include = os.path.join(home_dot_gyp, 'include.gypi')
    if os.path.exists(default_include):
      print 'Using overrides found in ' + default_include
      includes.append(default_include)

  # Command-line --include files come after the default include.
  if options.includes:
    includes.extend(options.includes)

  # Generator flags should be prefixed with the target generator since they
  # are global across all generator runs.
  gen_flags = []
  if options.use_environment:
    gen_flags += ShlexEnv('GYP_GENERATOR_FLAGS')
  if options.generator_flags:
    gen_flags += options.generator_flags
  generator_flags = NameValueListToDict(gen_flags)
  if DEBUG_GENERAL in gyp.debug.keys():
    DebugOutput(DEBUG_GENERAL, "generator_flags: %s", generator_flags)

  # Generate all requested formats (use a set in case we got one format request
  # twice)
  for format in set(options.formats):
    params = {'options': options,
              'build_files': build_files,
              'generator_flags': generator_flags,
              'cwd': os.getcwd(),
              'build_files_arg': build_files_arg,
              'gyp_binary': sys.argv[0],
              'home_dot_gyp': home_dot_gyp,
              'parallel': options.parallel,
              'root_targets': options.root_targets}

    # Start with the default variables from the command line.
    [generator, flat_list, targets, data] = Load(build_files, format,
                                                 cmdline_default_variables,
                                                 includes, options.depth,
                                                 params, options.check,
                                                 options.circular_check)

    # TODO(mark): Pass |data| for now because the generator needs a list of
    # build files that came in.  In the future, maybe it should just accept
    # a list, and not the whole data dict.
    # NOTE: flat_list is the flattened dependency graph specifying the order
    # that targets may be built.  Build systems that operate serially or that
    # need to have dependencies defined before dependents reference them should
    # generate targets in the order specified in flat_list.
    generator.GenerateOutput(flat_list, targets, data, params)

    if options.configs:
      valid_configs = targets[flat_list[0]]['configurations'].keys()
      for conf in options.configs:
        if conf not in valid_configs:
          raise GypError('Invalid config specified via --build: %s' % conf)
      generator.PerformBuild(data, options.configs, params)

  # Done
  return 0


def main(args):
  try:
    return gyp_main(args)
  except GypError, e:
    sys.stderr.write("gyp: %s\n" % e)
    return 1

# NOTE: setuptools generated console_scripts calls function with no arguments
def script_main():
  return main(sys.argv[1:])

if __name__ == '__main__':
  sys.exit(script_main())
