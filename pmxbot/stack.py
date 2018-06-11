"""!stack, a crunchbot command for managing short lists.

Extended Example
----------------

!stack
    (empty)
!stack add drop partitions
!stack
    1: drop partitions
!stack add fix up join diagrams
!stack
    1: fix up join diagrams
    2: drop partitions
!stack add review frank's ticket
!stack
    1: review frank's ticket
    2: fix up join diagrams
    3: drop partitions
!stack add [-1] crunchbot stack command
!stack
    1: review frank's ticket
    2: fix up join diagrams
    3: drop partitions
    4: crunchbot stack command
!stack add [2] review kim's ticket
!stack
    1: review frank's ticket
    2: review kim's ticket
    3: fix up join diagrams
    4: drop partitions
    5: crunchbot stack command
!stack pop
    -: review frank's ticket
!stack
    1: review kim's ticket
    2: fix up join diagrams
    3: drop partitions
    4: crunchbot stack command
!stack pop [-1]
    -: crunchbot stack command
!stack
    1: review kim's ticket
    2: fix up join diagrams
    3: drop partitions
!stack pop [:]
    -: drop partitions
    -: fix up join diagrams
    -: review kim's ticket
!stack
    (empty)

Topic and Index Parameters
--------------------------

Topic and index parameters are specified immediately after the !stack subcommand,
and take the form: `topic[index]`; for example, "meetup[3]". The square brackets
must always be included, even if no index is given. For example, "meetup[]"
identifies the "meetup" topic but gives no index parameter--what that means
depends on the subcommand. Similarly, you can specify "[3]" to give an index
with no topic.

If no topic is given, the user's nick is used as the topic.
This allows the most common use as a personal stack of work items.

The items in each topic are organized as a stack. Each item in the stack has
an index number; the top-most item is 1 and the bottom-most item is always
the length of the stack. With some restrictions depending on the command,
an "index" argument may be any of the following:

    * A single item index, like `[3]`. Negative indices count backward from
      the bottom; that is, the bottom-most item in a 3-item stack can be
      identified by `[3]` or `[-1]`.
    * A slice, shorthand for the entire inclusive range between two numbers,
      like `[3:5]`. Either number may be negative, or omitted to mean 1 or -1,
      respectively. If both are omitted as `[:]` then all items match.
    * Any "text" surrounded by single or double-quotes, which matches any
      item containing the text.
    * Any /text/ surrounded by forward-slashes, a regular expression
      to match item content.
    * The sentinel values first and last.
    * Any combination of the above, separated by commas; for example,
      given a stack of items
      "1: red | 2: orange | 3: yellow | 4: green | 5: blue | 6: indigo | 7: violet",
      the index `[6, :2, "i"]` identifies "6: indigo | 1: red | 2: orange | 7: violet".
      Note that "indigo" matches both `[6]` and `["i"]`, but is only included
      once. However, if the stack had another "8: indigo" entry, it would have
      been included.

Subcommands
-----------

!stack show        topic[index]
    Returns the list of items for the given topic.

!stack add         topic[index] <item>
    Adds the given item to the given topic before the given index(es).
    If no index is given, the default is [1] which adds to the front.
    Any index higher than the number of items adds to the end of the stack.

!stack pop         topic[index] <item>
    Removes items from the given topic at the given index(es).

    If index is omitted, it defaults to `[1]`.

!stack shuffle topic[index]
    Reorders the given topic.

    If the `index` argument is omitted, the topic is shuffled in random order.
    Otherwise, it must be a valid index and the topic is reordered to match.
    For example, with stack "1: a | 2: b | 3: c", the command `!stack shuffle [3, 1]`
    reorders the stack to "1: c | 2: a", and the "b" item is dropped.

"""

import random
import re

from . import storage
from .core import command

debug = False


class Stack(storage.SelectableStorage):
    @classmethod
    def init(cls):
        cls.store = cls.from_URI()
        cls._finalizers.append(cls.finalize)

    @classmethod
    def finalize(cls):
        del cls.store


class SQLiteStack(Stack, storage.SQLiteStorage):
    def init_tables(self):
        CREATE_STACK_TABLE = '''
            CREATE TABLE
            IF NOT EXISTS stack
            (
                topic VARCHAR NOT NULL,
                items VARCHAR NOT NULL,
                primary key(topic)
            )
        '''
        self.db.execute(CREATE_STACK_TABLE)
        self.db.commit()

    def get_items(self, topic):
        rows = self.db.execute("SELECT items FROM stack WHERE topic = ?", [topic])
        if not rows:
            return []
        else:
            return rows[0][0].split("\n")

    def save_items(self, topic, items):
        items = "\n".join(items)
        if not self.db.execute("SELECT items FROM stack WHERE topic = ?", [topic]):
            return self.db.execute(
                "INSERT INTO stack (topic, items) VALUES (?, ?)", [topic, items])
        else:
            return self.db.execute(
                "UPDATE stack SET items = ? WHERE topic = ?", [items, topic])


class MongoDBStack(Stack, storage.MongoDBStorage):
    collection_name = 'stack'

    def get_items(self, topic):
        doc = self.db.find_one({'topic': topic})
        if doc is None:
            return []
        else:
            return doc["items"]

    def save_items(self, topic, items):
        return self.db.update_one({"topic": topic}, {"$set": {"items": items}}, upsert=True)


help = {
    "stack": '!stack <subcommand> <topic[index]> <item> | subcommand: show, add, pop, shuffle, help | index: [2, 4:-3 (inclusive), "foo", /ba.*r/]',
    "help": "!stack help <show, add, pop, shuffle, help, stack, index>: Show help for the given subcommand or feature (default: help)",
    "add": "!stack add <topic[index]> item: Add the given item to the given topic before the given (1-based) index (default: 1)",
    "pop": "!stack pop <topic[index]>: Pop any items from the given topic at the given (1-based) index(es) (default: 1)",
    "show": "!stack show <topic[index]> <multiline>: Show items from the given topic at the given (1-based) indexes (default: all)",
    "shuffle": "!stack shuffle <topic[index]>: Shuffle items from the given topic into the the given (1-based) index order (default: random)",
    "index": '!stack indexes must be integers `[2]`, start:end slices (inclusive) `[4:-3]`, `"text"` or a `/regex/` to match, `first` or `last`, or any combination of those separated by commas.'
}


def parse_index(index, items):
    """Return a list of 0-based index numbers from the given (1-based) `index`.

    * A single item index, like `[3]`. Negative indices count backward from
      the bottom; that is, the bottom-most item in a 3-item stack can be
      identified by `[3]` or `[-1]`.
    * A slice, shorthand for the entire inclusive range between two numbers,
      like `[3:5]`. Either number may be negative, or omitted to mean 1 or -1,
      respectively. If both are omitted as `[:]` then all items match.
    * Any "text" surrounded by single or double-quotes, which matches any
      item containing the text (case-insensitive).
    * Any /text/ surrounded by forward-slashes, a regular expression
      to match item content.
    * The values "first" or "last" (without quotes).
    * Any combination of the above, separated by commas; for example,
      given a stack of items
      "1: red | 2: orange | 3: yellow | 4: green | 5: blue | 6: indigo | 7: violet",
      the index `[6, :2, "i"]` identifies "6: indigo | 1: red | 2: orange | 7: violet".
      Note that "indigo" matches both `[6]` and `["i"]`, but is only included
      once. However, if the stack had another "8: indigo" entry, it would have
      been included.

    """
    indices = []
    if index is None:
        return indices

    for atom in index.split(","):
        atom = atom.strip()
        if not atom:
            continue

        if (
            (atom.startswith("'") and atom.endswith("'")) or
            (atom.startswith('"') and atom.endswith('"'))
        ):
            atom = atom[1:-1].lower()
            for i, item in enumerate(items):
                if atom in item.lower():
                    indices.append(i)
        elif atom.startswith('/') and atom.endswith('/'):
            atom = atom[1:-1]
            for i, item in enumerate(items):
                if re.search(atom, item):
                    indices.append(i)
        elif ":" in atom:
            start, end = [x.strip() for x in atom.split(":", 1)]
            start = int(start) if start else 1
            if start < 0:
                start += len(items) + 1
            end = int(end) if end else len(items)
            if end < 0:
                end += len(items) + 1
            start -= 1  # Shift to Python 0-based indices
            end -= 1    # Shift to Python 0-based indices
            for i in range(start, end + 1):
                indices.append(i)
        elif atom == "first":
            indices.append(0)
        elif atom == "last":
            indices.append(len(items) - 1)
        else:
            index = int(atom)
            if index < 0:
                index += len(items) + 1
            index -= 1  # Shift to Python 0-based indices
            indices.append(index)

    return indices


@command()
def stack(nick, rest):
    atoms = [atom.strip() for atom in rest.split(' ', 1) if atom.strip()]
    if len(atoms) == 0:
        subcommand = "show"
        rest = ""
    elif len(atoms) == 1:
        subcommand = atoms[0]
        rest = ""
    else:
        subcommand, rest = atoms

    start = rest.find("[")
    finish = rest.rfind("]")
    sp = rest.find(" ")
    if start != -1 and finish != -1 and start < finish and (sp == -1 or start < sp):
        topic, index = [atom.strip() for atom in rest[:finish].split("[", 1)]
        if not topic:
            topic = nick
        new_item = rest[finish + 1:].strip()
    else:
        topic = nick
        index = None
        new_item = rest.strip()

    items = Stack.store.get_items(topic)
    try:
        indices = parse_index(index, items)
    except ValueError:
        return help["index"]

    if debug:
        print("SUBCOMMAND", subcommand.ljust(8), "TOPIC", topic.ljust(8), "INDICES", str(indices).ljust(12), "ITEM", new_item)

    if subcommand == "add":
        if not new_item:
            return '!stack add <topic[index]> item: You must provide an item to add.'

        if not indices:
            items.insert(0, new_item)
        else:
            for i in reversed(sorted(set(indices))):
                if i >= len(items):
                    items.append(new_item)
                else:
                    items.insert(i + 1, new_item)

        Stack.store.save_items(topic, items)
    elif subcommand == "pop":
        if not indices:
            indices = [0]

        popped_items = [items.pop(i) for i in reversed(sorted(set(indices)))
                        if len(items) > i >= 0]

        Stack.store.save_items(topic, items)

        return " | ".join(["-: %s" % (item,) for item in reversed(popped_items)]) or "(none popped)"
    elif subcommand == "show":
        sep = " | "
        if new_item:
            if "multiline".startswith(new_item):
                sep = "\n"
            else:
                return '!stack show <topic[index]> <multiline>: Show items from the given topic at the given (1-based) indexes (default: all)'

        if not indices:
            indices = range(len(items))

        return sep.join(["%d: %s" % (i + 1, items[i]) for i in indices if len(items) > i >= 0]) or "(empty)"
    elif subcommand == "shuffle":
        if not indices:
            random.shuffle(items)
        else:
            items = [items[i] for i in indices if len(items) > i >= 0]

        Stack.store.save_items(topic, items)
        return " | ".join(["%d: %s" % (i, item) for i, item in enumerate(items, 1)]) or "(empty)"
    elif subcommand == "help":
        return help.get(new_item, help["help"])
    else:
        return help["stack"]
