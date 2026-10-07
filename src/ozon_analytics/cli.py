import argparse
import logging

from .collector import collect
from .database import migrate
from .operations import backup, check_backup, monitor


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['migrate','collect','monitor','backup','check-backup'])
    parser.add_argument('path',nargs='?')
    args=parser.parse_args()
    if args.command=='check-backup':
        check_backup(args.path)
        print('Backup checksum verified')
    else:
        {'migrate':migrate,'collect':collect,'monitor':monitor,'backup':backup}[args.command]()


if __name__=='__main__':
    main()
