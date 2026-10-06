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
