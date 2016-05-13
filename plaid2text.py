#! /usr/bin/env python3

# Access account information from Plaid.com accounts
# and generate ledger/beancount formatted file.
#
# Requires Python >=3.2 MongoDB >= 3.2.3 and (Ledger >=3.0 OR beancount >= 2.0)

# Ideas and Code heavily borrowed (read: shamelessly stolen) from the awesome: icsv2ledger
# https://github.com/quentinsf/icsv2ledger
#
import re
import argparse
import sys
from renderers import LedgerRenderer,BeancountRenderer
import config_manager as cm
import storage_manager
from online_accounts import PlaidAccess
from argparse import HelpFormatter
from datetime import datetime
from operator import attrgetter


class FileType(object):
    """Based on `argparse.FileType` from python3.4.2, but with additional
    support for the `newline` parameter to `open`.
    """

    def __init__(self,
                 mode='r',
                 bufsize=-1,
                 encoding=None,
                 errors=None,
                 newline=None):
        self._mode = mode
        self._bufsize = bufsize
        self._encoding = encoding
        self._errors = errors
        self._newline = newline

    def __call__(self, string):
        # the special argument "-" means sys.std{in,out}
        if string == '-':
            if 'r' in self._mode:
                return sys.stdin
            elif 'w' in self._mode:
                return sys.stdout
            else:
                msg = 'argument "-" with mode %r' % self._mode
                raise ValueError(msg)

        # all other arguments are used as file names
        try:
            return open(string,
                        self._mode,
                        self._bufsize,
                        self._encoding,
                        self._errors,
                        newline=self._newline)
        except OSError as e:
            message = "can't open '%s': %s"
            raise ArgumentTypeError(message % (string, e))

    def __repr__(self):
        args = self._mode, self._bufsize
        kwargs = [('encoding', self._encoding), ('errors', self._errors),
                  ('newline', self._newline)]
        args_str = ', '.join([repr(arg) for arg in args if arg != -1] +
                             ['%s=%r' % (kw, arg)
                              for kw, arg in kwargs if arg is not None])
        return '%s(%s)' % (type(self).__name__, args_str)


class SortingHelpFormatter(HelpFormatter):
    """Sort options alphabetically when -h prints usage
    See http://stackoverflow.com/questions/12268602
    """

    def add_arguments(self, actions):
        actions = sorted(actions, key=attrgetter('option_strings'))
        super(SortingHelpFormatter, self).add_arguments(actions)


def _create_plaid_account(nickname):
    """
    Guides the user through creating a new Plaid connect account
    """
    pa = PlaidAccess()
    pa.add_account(nickname)


def _parse_args_and_config_file():
    """ Read options from config file and CLI args
    1. Reads hard coded cm.CONFIG_DEFAULTS
    2. Supersedes by values in config file
    3. Supersedes by values from CLI args
    """

    # Build preparser with only plaid account
    preparser = argparse.ArgumentParser(prog='Plaid2Text',add_help=False)
    preparser.add_argument('plaid_account',
                           nargs='?',
                           help=('Nickname of Plaid account to use'
                                 ' (Example: {0})'.format('boa_checking')))

    preparser.add_argument('outfile',
                        nargs='?',
                        metavar='FILE',
                        type=FileType('w', encoding='utf-8'),
                        default=sys.stdout,
                        help=('output filename or stdout in Ledger/Beancount syntax'
                              ' (default: {0})'.format('stdout')))

    preparser.add_argument(
        '--create-account',
        action='store_true',
        help=
        ('Create a new Plaid account using the plaid-account argument as the new nickname'
         ' (Example: {0})'.format('chase_savings')))
    # Parse args with preparser, and find config file
    args, remaining_argv = preparser.parse_known_args()

    if args.create_account and args.plaid_account:
        if cm.account_exists(args.plaid_account):
            print(
                'Config file {0} already contains section for account: {1}\n\n\
            You will have to MANUALLY delete it if you want to recreate it.'
                .format(cm.FILE_DEFAULTS.config_file, args.plaid_account),
                file=sys.stderr)
            sys.exit(1)
        else:
            _create_plaid_account(args.plaid_account)
            print('New account {} successfully created.'.format(
                args.plaid_account),
                  file=sys.stdout)
            sys.exit(0)
            return

    defaults = cm.get_config(args.plaid_account) if args.plaid_account else {}
    # defaults = cm.CONFIG_DEFAULTS

    # Build parser for args on command line
    parser = argparse.ArgumentParser(prog='Plaid2Text',
        # Don't surpress add_help here so it will handle -h
        # print script description with -h/--help
        description=__doc__,
                                     parents=[preparser],
        # sort options alphabetically
        formatter_class=SortingHelpFormatter)

    parser.set_defaults(**defaults)
    parser.add_argument(
        '--accounts-file',
        metavar='FILE',
        help=('file which holds a list of account names (LEDGER ONLY)'
              ' (default : {0})'.format(cm.FILE_DEFAULTS.accounts_file)))
    parser.add_argument(
        '--headers-file',
        metavar='FILE',
        help=('file which contains contents to be written to the top of the output file'
              ' (default : {0})'.format(cm.FILE_DEFAULTS.headers_file)))

    parser.add_argument(
        '--output-format',
        '-o',
        choices=['beancount','ledger'],
        help=('what format to use for the output file.'
         ' (default format: {})'.format(cm.CONFIG_DEFAULTS.output_format)))
    parser.add_argument(
        '--posting-account',
        '-a',
        metavar='STR',
        help=('posting account used as source'
              ' (default: {0})'.format(cm.CONFIG_DEFAULTS.posting_account)))

    parser.add_argument(
        '--journal-file',
        '-j',
        metavar='FILE',
        help=
        ('journal file where to read payees/accounts\nTip: you can use includes to pull in your other journal files'
         ' (default journal file: {0})'.format(cm.FILE_DEFAULTS.journal_file)))
    parser.add_argument(
        '--quiet',
        '-q',
        action='store_true',
        help=('do not prompt if account can be deduced from mappings'
              ' (default: {0})'.format(cm.CONFIG_DEFAULTS.quiet)))
    parser.add_argument(
        '--download-transactions',
        '-d',
        action='store_true',
        help=('download transactions into Mongo for given plaid account'))
    parser.add_argument(
        '--mongo-db',
        metavar='STR',
        help=('The name of the Mongo database'
              ' (default: {0})'.format(cm.CONFIG_DEFAULTS.mongo_db)))
    parser.add_argument(
        '--default-expense',
        metavar='STR',
        help=('expense account used as default destination'
              ' (default: {0})'.format(cm.CONFIG_DEFAULTS.default_expense)))
    parser.add_argument(
        '--cleared-character',
        choices='*!',
        help=('character to clear a transaction'
              ' (default: {0})'.format(cm.CONFIG_DEFAULTS.cleared_character)))

    parser.add_argument('--output-date-format',
                        metavar='STR',
                        help=('date format for output file'
                              ' (default: YYYY/MM/DD)'))

    parser.add_argument(
        '--currency',
        metavar='STR',
        help=('the currency of amounts'
              ' (default: {0})'.format(cm.CONFIG_DEFAULTS.currency)))

    parser.add_argument('--mapping-file',
                        metavar='FILE',
                        help=('file which holds the mappings'
                              ' (default: {0})'
                              .format(cm.FILE_DEFAULTS.mapping_file)))
    parser.add_argument('--template-file',
                        metavar='FILE',
                        help=('file which holds the template'
                              ' (default: {0})'
                              .format(cm.FILE_DEFAULTS.template_file)))
    parser.add_argument(
        '--tags',
        '-t',
        action='store_true',
        help=('prompt for transaction tags'
              ' (default: {0})'.format(cm.CONFIG_DEFAULTS.tags)))
    parser.add_argument(
        '--clear-screen',
        '-C',
        action='store_true',
        help=('clear screen for every transaction'
              ' (default: {0})'.format(cm.CONFIG_DEFAULTS.clear_screen)))
    parser.add_argument(
        '--no-mark-processed',
        '-n',
        action='store_false',
        help=('Do not mark pulled transactions. '
              'When given, the pulled transactions will still be listed as new transactions upon the next run.'
              ' (default: False)'))

    parser.add_argument(
        '--all-transactions',
        action='store_true',
        help=
        ('pull all transactions even those who have been previously marked as processed'
         ' (default: False'))

    parser.add_argument(
        '--to-date',
        metavar='STR',
        help=
        ('specify the ending date for transactions to be pulled; use in conjunction with --from-date to specify range'
         'Date format: YYYY-MM-DD'))

    parser.add_argument(
        '--from-date',
        metavar='STR',
        help=
        ('specify a the starting date for transactions to be pulled; use in conjunction with --to-date to specify range'
         'Date format: YYYY-MM-DD'))

    #NEED TO FIX - USING PARENTS causes file to be opened twice
    args = parser.parse_args()

    args.journal_file = cm.find_first_file(args.journal_file,
                                          cm.FILE_DEFAULTS.journal_file)
    args.mapping_file = cm.find_first_file(args.mapping_file,
                                           cm.FILE_DEFAULTS.mapping_file)
    args.accounts_file = cm.find_first_file(args.accounts_file,
                                            cm.FILE_DEFAULTS.accounts_file)
    args.template_file = cm.find_first_file(args.template_file,
                                            cm.FILE_DEFAULTS.template_file)
    args.headers_file = cm.find_first_file(args.headers_file,
                                            cm.FILE_DEFAULTS.headers_file)
    #make sure we have a plaid account and we are not calling --help
    if not args.plaid_account and not 'help' in args:
        print('You must provide the Plaid account as the first argument',
              file=sys.stderr)
        sys.exit(1)

    if args.from_date:
        y, m, d = [int(i) for i in re.split(r'[/-]',args.from_date)]
        args.from_date = datetime(y, m, d)

    if args.to_date:
        y, m, d = [int(i) for i in re.split(r'[/-]',args.to_date)]
        args.to_date = datetime(y, m, d)

    return args


def main():

    #make sure we have config file
    if not cm.config_exists():
        return

    options = _parse_args_and_config_file()
    truthy = ['true','yes','1','t']
    # convert config values to Boolean if pulled from file
    if not isinstance(options.quiet,bool):
        options.quiet = options.quiet.lower() in truthy
    if not isinstance(options.tags,bool):
        options.tags = options.tags.lower() in truthy
    if not isinstance(options.clear_screen,bool):
        options.clear_screen = options.clear_screen.lower() in truthy

    sm = storage_manager.StorageManager(options.mongo_db, options.plaid_account,options.posting_account)

    if options.download_transactions:
        trans = PlaidAccess().get_transactions(options.access_token, options.account)
        sm.save_transactions(trans)
        print("Transactions successfully downloaded and saved into Mongo",file=sys.stdout)
        sys.exit(0)

    if not options.config_file:
        print("Configuration file is required.",file=sys.stderr)
        sys.exit(1)


    to_date = None if not 'to_date' in options else options.to_date
    from_date = None if not 'from_date' in options else options.from_date
    only_new = not options.all_transactions

    trxs = sm.get_transactions(to_date=to_date,
                               from_date=from_date,
                               only_new=only_new)
    # for t in trxs:
    #     print(t)

    #     return

    if options.output_format == 'beancount':
        out = BeancountRenderer(trxs,options)
    else:
        out = LedgerRenderer(trxs,options)

    update_dict = out.process_transactions()
    if options.no_mark_processed:
        for u in update_dict:
            sm.update_transaction(u)


if __name__ == "__main__":
    main()
