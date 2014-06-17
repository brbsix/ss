from __future__ import print_function, division
from contextlib import closing
import gzip
import optparse
import os
import shutil
import struct
import tempfile
import time
import sys

import guessit
import subprocess


if sys.version_info[0] == 3:
    from urllib.request import urlopen
    from xmlrpc.client import ServerProxy
    from configparser import RawConfigParser
else:
    from urllib import urlopen
    from xmlrpclib import Server as ServerProxy
    from ConfigParser import RawConfigParser


def obtain_guessit_query(movie_filename, language):
    guess = guessit.guess_file_info(os.path.basename(movie_filename), info=['filename'])

    def extract_query(guess, parts):
        result = ['"%s"' % guess.get(k) for k in parts if guess.get(k)]
        return ' '.join(result)

    result = {}
    if guess.get('type') == 'episode':
        result['query'] = extract_query(guess, ['series', 'title', 'releaseGroup'])
        if 'season' in guess:
            result['season'] = guess['season']
        if 'episodeNumber' in guess:
            result['episode'] = guess['episodeNumber']

    elif guess.get('type') == 'movie':
        result['query'] = extract_query(guess, ['title', 'year'])
    else:
        assert 'guessit returned invalid query:'
        result['query'] = os.path.basename(movie_filename)

    result['sublanguageid'] = language

    return result


def obtain_movie_hash_query(movie_filename, language):
    return {
        'moviehash': calculate_hash_for_file(movie_filename),
        'moviebytesize': str(os.path.getsize(movie_filename)),
        'sublanguageid': language,
    }


def filter_bad_results(search_results, guessit_query):
    # filter out search results with bad season and episode number (if applicable);
    # sometimes OpenSubtitles will report search results subtitles that belong
    # to a different episode or season from a tv show; no reason why, but it seems to
    # work well just filtering those out
    if 'season' in guessit_query and 'episode' in guessit_query:
        guessit_season_episode = (guessit_query['season'], guessit_query['episode'])
        search_results = [x for x in search_results
                          if (int(x['SeriesSeason']), int(x['SeriesEpisode'])) == guessit_season_episode]
    return search_results


def query_open_subtitles(movie_filenames, language):
    uri = 'http://api.opensubtitles.org/xml-rpc'
    server = ServerProxy(uri, verbose=0, allow_none=True, use_datetime=True)
    login_info = server.LogIn('', '', 'en', 'OS Test User Agent')
    token = login_info['token']

    try:
        result = {}

        for movie_filename in movie_filenames:
            guessit_query = obtain_guessit_query(movie_filename, language)
            search_queries = [
                guessit_query,
                obtain_movie_hash_query(movie_filename, language),
            ]

            response = server.SearchSubtitles(token, search_queries)
            search_results = response['data']

            if search_results:
                search_results = filter_bad_results(search_results, guessit_query)
                result[movie_filename] = search_results

        return result
    finally:
        server.LogOut(token)


def find_subtitles(movie_filenames, language):
    all_search_results = query_open_subtitles(movie_filenames, language)

    for movie_filename in movie_filenames:
        search_results = all_search_results.get(movie_filename, [])
        if search_results:
            search_result = search_results[0]
            yield movie_filename, search_result['SubDownloadLink'], '.' + search_result['SubFormat']
        else:
            yield movie_filename, None, None


def obtain_subtitle_filename(movie_filename, language, subtitle_ext):
    dirname = os.path.dirname(movie_filename)
    basename = os.path.splitext(os.path.basename(movie_filename))[0]

    # possibilities where we don't override
    filenames = [
        #  -> movie.srt
        os.path.join(dirname, basename + subtitle_ext),
        #  -> movie.eng.srt
        os.path.join(dirname, '%s.%s%s' % (basename, language, subtitle_ext)),
    ]
    for filename in filenames:
        if not os.path.isfile(filename):
            return filename

    # use also ss on the extension and always overwrite
    #  -> movie.eng.ss.srt
    return os.path.join(dirname, '%s.%s.%s%s' % (basename, language, 'ss', subtitle_ext))


def download_subtitle(subtitle_url, subtitle_filename):
    # first download it and save to a temp dir
    with closing(urlopen(subtitle_url)) as urlfile:
        gzip_subtitle_contents = urlfile.read()

    tempdir = tempfile.mkdtemp()
    try:
        basename = subtitle_url.split('/')[-1]
        tempfilename = os.path.join(tempdir, basename)
        with open(tempfilename, 'wb') as f:
            f.write(gzip_subtitle_contents)

        with closing(gzip.GzipFile(tempfilename, 'rb')) as f:
            subtitle_contents = f.read()

        # copy it over the new filename
        with open(subtitle_filename, 'wb') as f:
            f.write(subtitle_contents)
    finally:
        shutil.rmtree(tempdir)


def find_movie_files(input_names, recursive=False):
    extensions = set(['.avi', '.mp4', '.mpg', '.mkv'])
    returned = set()

    for input_name in input_names:

        if os.path.isfile(input_name) and input_name not in returned:
            yield input_name
            returned.add(input_name)
        else:
            names = os.listdir(input_name)
            for name in names:
                result = os.path.join(input_name, name)
                if name[-4:] in extensions:
                    if result not in returned:
                        yield result
                        returned.add(result)

                elif os.path.isdir(result) and recursive:
                    for x in find_movie_files([result], recursive):
                        yield x


def has_subtitle(filename):
    # list of subtitle formats obtained from opensubtitles' advanced search page.
    formats = ['.sub', '.srt', '.ssa', '.smi', '.mpl']
    basename = os.path.splitext(filename)[0]
    for ext in formats:
        if os.path.isfile(basename + ext):
            return True

    return False


def _change_configuration(params, filename):
    config = load_configuration(filename)
    config.set_config_from_lines(params)

    with open(filename, 'w') as f:
        for line in config.get_lines():
            f.write(line + '\n')

    return config


def load_configuration(filename):
    p = RawConfigParser()
    p.add_section('ss')
    p.read(filename)

    def read_if_defined(option, getter):
        if p.has_option('ss', option):
            value = getattr(p, getter)('ss', option)
            setattr(config, option, value)

    config = Configuration()
    read_if_defined('language', 'get')
    read_if_defined('recursive', 'getboolean')
    read_if_defined('skip', 'getboolean')
    read_if_defined('mkv', 'getboolean')
    return config


def calculate_hash_for_file(name):
    '''
    Calculates the hash for the given filename.

    Algorithm from: http://trac.opensubtitles.org/projects/opensubtitles/wiki/HashSourceCodes

    @param name: str
        Path to the file

    @return: str
        The calculated hash code, as an hex string.
    '''
    longlongformat = 'q'  # long long
    bytesize = struct.calcsize(longlongformat)

    f = open(name, "rb")

    filesize = os.path.getsize(name)
    hash = filesize

    if filesize < 65536 * 2:
        return "SizeError"

    for x in range(65536//bytesize):
        buffer = f.read(bytesize)
        (l_value,)= struct.unpack(longlongformat, buffer)
        hash += l_value
        hash = hash & 0xFFFFFFFFFFFFFFFF #to remain as 64bit number


    f.seek(max(0,filesize-65536),0)
    for x in range(65536//bytesize):
        buffer = f.read(bytesize)
        (l_value,)= struct.unpack(longlongformat, buffer)
        hash += l_value
        hash = hash & 0xFFFFFFFFFFFFFFFF

    f.close()
    returnedhash = "%016x" % hash
    return returnedhash


class Configuration(object):

    def __init__(self, language='eng', recursive=False, skip=False, mkv=False):
        self.language = language
        self.recursive = recursive
        self.skip = skip
        self.mkv = mkv

    def __eq__(self, other):
        return \
            self.language == other.language and \
            self.recursive == other.recursive and \
            self.skip == other.skip and \
            self.mkv == other.mkv

    def __ne__(self, other):
        return not self == other

    def __repr__(self):
        return 'Configuration(language="%s", recursive=%s, skip=%s, mkv=%s)' % \
               (self.language, self.recursive, self.skip, self.mkv)

    def __str__(self):
        values = [
            'language = %s' % self.language,
            'recursive = %s' % self.recursive,
            'skip = %s' % self.skip,
            'mkv = %s' % self.mkv,
        ]
        return '\n'.join(values)

__version__ = '1.4.2'

def main(argv=None, stream=sys.stdout):
    if argv is None:
        argv = sys.argv
    parser = optparse.OptionParser(
        usage='Usage: ss [options] <file or dir> <file or dir>...',
        description='Searches for subtitles using OpenSubtitles (http://www.opensubtitles.org).\n\nVersion: %s' % __version__,
        epilog='If a directory is given, search for subtitles for all movies on it (non-recursively).',
    )
    parser.add_option('-v', '--verbose',
                      help='always displays configuration and enable verbose mode.',
                      action='store_true', default=False)
    options, args = parser.parse_args(args=argv)

    config_filename = os.path.join(os.path.expanduser('~'), '.ss.ini')
    config = load_configuration(config_filename)
    if options.verbose:
        print('Configuration read from {0}'.format(config_filename))
        print(config, file=stream)
        print()

    if len(args) < 2:
        parser.print_help(file=stream)
        return 2

    input_filenames = list(find_movie_files(args[1:], recursive=config.recursive))
    if not input_filenames:
        print('No files to search subtitles for. Aborting.', file=stream)
        return 1

    if config.mkv:
        if not check_mkv():
            print('mkvmerge not found in PATH.', file=stream)
            print('Either install mkvtoolnix or disable mkv merging ' +
                  'in your config.', file=stream)
            return 4

    skipped_filenames = []
    if config.skip:
        new_input_filenames = []
        for input_filename in input_filenames:
            if has_subtitle(input_filename):
                skipped_filenames.append(input_filename)
            else:
                new_input_filenames.append(input_filename)
        input_filenames = new_input_filenames

        if skipped_filenames:
            print('Skipping %d files that already have subtitles.' % len(
                skipped_filenames), file=stream)

    def print_status(text, status):
        spaces = 70 - len(text)
        if spaces < 2:
            spaces = 2
        print('%s%s%s' % (text, ' ' * spaces, status), file=stream)


    print('Language: %s' % config.language, file=stream)

    if not input_filenames:
        return 1

    print('Querying OpenSubtitles.org for %d file(s)...' % len(input_filenames), file=stream)
    print('', file=stream)

    matches = []
    for (movie_filename, subtitle_url, subtitle_ext) in sorted(
            find_subtitles(input_filenames, language=config.language)):
        if subtitle_url:
            status = 'OK'
        else:
            status = 'No matches found.'

        print_status('- %s' % os.path.basename(movie_filename), status)

        if subtitle_url:
            subtitle_filename = obtain_subtitle_filename(movie_filename, config.language, subtitle_ext)
            matches.append((movie_filename, subtitle_url, subtitle_ext, subtitle_filename))

    if not matches:
        return 0

    print('', file=stream)
    print('Downloading...', file=stream)
    for (movie_filename, subtitle_url, subtitle_ext, subtitle_filename) in matches:
        download_subtitle(subtitle_url, subtitle_filename)
        print_status(' - %s' % os.path.basename(subtitle_filename), 'DONE')

    if config.mkv:
        print('', file=stream)
        print('Embedding MKV...', file=stream)
        failures = []  # list of (movie_filename, output)
        for (movie_filename, subtitle_url, subtitle_ext, subtitle_filename) in matches:
            if os.path.splitext(movie_filename)[1].lower() != u'.mkv':
                status, output = embed_mkv(movie_filename, subtitle_filename, config.language)
                output_filename = os.path.splitext(movie_filename)[0] + u'.mkv'
                if not status:
                    failures.append((movie_filename, output))
                status = 'DONE' if status else 'ERROR'
                print_status(' - %s' % os.path.basename(output_filename),
                             status)
            else:
                print_status(' - %s' % os.path.basename(movie_filename),
                             'skipped')

        if failures:
            header = print('_' * 80, file=stream)
            print(header, file=stream)
            for movie_filename, output in failures:
                print(':{%s}:' % movie_filename, file=stream)
                print(output, file=stream)

    return 0


def embed_mkv(movie_filename, subtitle_filename, language):
    output_filename = os.path.splitext(movie_filename)[0] + u'.mkv'
    params = [
        u'mkvmerge',
        u'--output', output_filename,
        movie_filename,
        u'--language', u'0:{0}'.format(language),
        subtitle_filename,
    ]
    try:
        check_output(params)
    except subprocess.CalledProcessError as e:
        return False, e.output
    else:
        return True, ''


def check_mkv():
    """
    Returns True if mkvtoolinx seems to be installed.
    """
    try:
        check_output([u'mkvmerge', u'--version'])
    except subprocess.CalledProcessError:
        return False
    else:
        return True


def check_output(params):
    """
    Python 2.6 support: subprocess.check_output from Python 2.7.
    """
    popen = subprocess.Popen(params, shell=True, stderr=subprocess.STDOUT,
                             stdout=subprocess.PIPE)

    output, _ = popen.communicate()
    returncode = popen.poll()
    if returncode != 0:
        raise subprocess.CalledProcessError(returncode=returncode, cmd=params,
                                            output=output)
    return output


if __name__ == '__main__':
    try:
        sys.exit(main())
    except:
        import traceback

        with open(__file__ + '.log', 'a+') as log_file:
            log_file.write('ERROR ' + ('=' * 80) + '\n')
            log_file.write('Date: %s' % time.strftime('%c'))
            log_file.write('args: ' + repr(sys.argv))
            traceback.print_exc(file=log_file)
        raise



