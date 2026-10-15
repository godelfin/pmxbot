"""Local, explicit administrator bootstrap: python -m pmxbot.users_cli."""

import argparse
from contextlib import closing

from .users import UserError, UserStore


def run(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, help='Main SQLite URI or filename')
    parser.add_argument('username', help='Existing enabled user to make administrator')
    args = parser.parse_args(argv)
    try:
        with closing(UserStore(args.database)) as store:
            user = store.bootstrap_admin(args.username)
    except UserError as exc:
        parser.error(str(exc))
    print(f'Administrator assigned: {user.username}')


if __name__ == '__main__':
    run()
