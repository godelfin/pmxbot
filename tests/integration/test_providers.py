"""Opt-in smoke checks; provider wording is deliberately not asserted."""

import pytest

from pmxbot import commands, util

pytestmark = [pytest.mark.integration, pytest.mark.network]


def test_acronym(needs_internet):
    assert util.lookup_acronym('IRC')


def test_autoinsult(needs_internet):
    assert commands.get_insult()


def test_urban_dictionary(needs_internet):
    assert util.urban_lookup('irc')


def test_wordnik(needs_wordnik):
    assert util.lookup('keyboard')


def test_emergency_compliment(needs_internet):
    assert util.load_emergency_compliments()
