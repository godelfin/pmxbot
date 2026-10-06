import json

import pytest

from pmxbot.music import album_prompt


def prompt(band, title):
    return album_prompt(
        {
            'artist_name': band,
            'title': title,
            'format': None,
            'format_description': None,
            'genre': None,
            'description': None,
        }
    )


@pytest.mark.parametrize(
    'band,title',
    [
        ('Bass and Brass', 'Classic'),
        ('Passion', 'Grape'),
        ('Cocktail', 'Analysis'),
    ],
)
def test_unambiguous_names_have_no_disclaimer(band, title):
    assert prompt(band, title) == (
        f'an album cover for the band "{band}". the name of the album is "{title}".'
    )


@pytest.mark.parametrize(
    'band,title,instruction',
    [
        ('The BREAST-of-Chicken', 'Sunday Dinner', 'no nudity or sexual anatomy'),
        ('Band', 'Breast of Chicken', 'no nudity or sexual anatomy'),
        ('Band', 'Sexy Rhythm', 'Do not depict sexual acts'),
        ('Band', 'Assault on Silence', 'Do not depict sexual violence'),
        ('Band', 'History of Nazis', 'Do not depict hateful imagery'),
    ],
)
def test_relevant_title_disclaimer(band, title, instruction):
    assert instruction in prompt(band, title)


def test_disclaimer_is_specific_to_matched_context():
    value = prompt('Band', 'Sexy Rhythm')
    assert 'sexual acts' in value
    assert 'sexual anatomy' not in value
    assert 'sexual violence' not in value
    assert 'hateful imagery' not in value


def test_embedded_quotes_are_escaped():
    band, title = 'The "Band"', 'An "Album"'
    assert prompt(band, title) == (
        f'an album cover for the band {json.dumps(band)}. '
        f'the name of the album is {json.dumps(title)}.'
    )


@pytest.mark.parametrize('word', ['torta', 'tortas', 'TORTA', 'Tortas'])
def test_torta_album_clarification(word):
    value = prompt('Band', f'{word} Aid')
    assert value.endswith(' Torta is slang for "thicc Latina".')
    assert 'band members' not in value


@pytest.mark.parametrize('word', ['torta', 'tortas', 'TORTA', 'Tortas'])
def test_torta_band_clarification(word):
    assert prompt(f'The {word}-Players', 'Album').endswith(
        ' Torta is slang for "thicc Latina" and describes the band members.'
    )


def test_torta_in_both_names_adds_both_clarifications():
    assert prompt('The Tortas', 'Torta Aid').endswith(
        ' Torta is slang for "thicc Latina".'
        ' Torta is slang for "thicc Latina" and describes the band members.'
    )


def test_torta_substrings_have_no_clarification():
    assert 'Torta is slang' not in prompt('Tortastic', 'Retorta')


@pytest.mark.parametrize(
    'band,title',
    [
        ('Medve', 'Album'),
        ('Band', 'Medve Returns'),
        ('The MEDVE-Players', 'Album'),
        ('Medve', 'medve'),
    ],
)
def test_medve_clarification(band, title):
    clarification = (
        "Medve isn't a bear, he's a middle aged white man with glasses "
        "and a beard and a lopsided grin."
    )
    value = prompt(band, title)
    assert value.endswith(' ' + clarification)
    assert value.count(clarification) == 1


def test_medve_substrings_have_no_clarification():
    assert "Medve isn't a bear" not in prompt('Medved', 'Medveville')


def test_medve_and_torta_clarifications_combine():
    value = prompt('Medve', 'Torta Aid')
    assert 'Torta is slang for "thicc Latina".' in value
    assert "Medve isn't a bear" in value
