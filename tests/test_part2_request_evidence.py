"""Synthetic, read-only DAT source integration contract (not live imports)."""
from dataclasses import dataclass
import unittest

from romarr.request_evidence import evaluate_source_request


@dataclass(frozen=True)
class Match:
    status: str = 'verified'
    game: str = 'Super Metroid (USA)'
    dat: str = 'Nintendo - SNES'


@dataclass(frozen=True)
class Group:
    members: tuple[str, ...]


class Source:
    def __init__(self, hashes=None):
        self.hashes = hashes or {'Super Metroid.sfc': {'crc': 'abcd1234', 'size': 4}}
        self.calls = []

    def hash(self, member, suffix):
        self.calls.append((member, suffix))
        item = self.hashes[member]
        if isinstance(item, Exception):
            raise item
        return item


class Dat:
    def __init__(self, mapping=None, legacy=None, required_roms=1):
        self.mapping = mapping or {'abcd1234': (Match(),)}
        self.legacy = legacy or Match(status='unknown')
        self.required_roms = required_roms

    def game_rom_count(self, match):
        return self.required_roms

    def verified_candidates(self, **hashes):
        return self.mapping.get(hashes.get('crc'), ())

    def lookup(self, **hashes):
        return self.legacy


class EvidenceTests(unittest.TestCase):
    def evaluate(self, source=None, dats=None, groups=None, name='Super Metroid', platform='snes', mapping=None):
        return evaluate_source_request(
            source or Source(),
            [Group(('Super Metroid.sfc',))] if groups is None else groups,
            dats or Dat(), name, platform,
            catalog_platforms={'Nintendo - SNES':'snes'} if mapping is None else mapping)

    def test_correct_game_and_platform_authorized(self):
        assert self.evaluate().decision.action == 'import'

    def test_match_has_original_verified_record(self):
        assert self.evaluate().verification == Match()

    def test_hashing_is_once_per_member(self):
        source = Source()
        self.evaluate(source=source)
        assert source.calls == [('Super Metroid.sfc', '.sfc')]

    def test_wrong_game_held(self):
        assert self.evaluate(name='Metroid Fusion').decision.code == 'REQUEST_TITLE_MISMATCH'

    def test_wrong_platform_held(self):
        assert self.evaluate(platform='gba').decision.code == 'DAT_PLATFORM_MISMATCH'

    def test_unmapped_dat_held(self):
        assert self.evaluate(mapping={}).decision.code == 'DAT_PLATFORM_UNCONFIRMED'

    def test_non_dict_map_held(self):
        assert self.evaluate(mapping=[1,2]).decision.code == 'DAT_PLATFORM_UNCONFIRMED'

    def test_empty_member_group_held(self):
        assert self.evaluate(groups=[Group(())]).decision.code == 'SOURCE_INCOMPLETE'

    def test_multi_group_held_before_hashing(self):
        source = Source()
        assert self.evaluate(source=source,groups=[Group(('x',)),Group(('y',))]).decision.code == 'AMBIGUOUS_MATCH'
        assert source.calls == []

    def test_no_dat_held(self):
        class NoIndex: pass
        assert self.evaluate(dats=NoIndex()).decision.code == 'DAT_UNKNOWN'

    def test_unknown_dat_is_hold(self):
        dat = Dat(mapping={'abcd1234': ()})
        assert self.evaluate(dats=dat).decision.code == 'DAT_UNKNOWN'

    def test_bad_checksum_is_reject(self):
        dat = Dat(mapping={'abcd1234': ()},legacy=Match(status='bad-dump'))
        assert self.evaluate(dats=dat).decision.code == 'DAT_FAILED'

    def test_unreadable_member_held(self):
        source = Source({'Super Metroid.sfc': OSError('simulated')})
        assert self.evaluate(source=source).decision.code == 'SOURCE_INCOMPLETE'

    def test_missing_hash_held(self):
        source = Source({'Super Metroid.sfc': {}})
        assert self.evaluate(source=source).decision.code == 'SOURCE_INCOMPLETE'

    def test_ambiguous_catalog_held(self):
        dat = Dat(mapping={'abcd1234': (Match(), Match(game='Metroid Fusion',dat='Nintendo - GBA'))})
        assert self.evaluate(dats=dat).decision.code == 'DAT_PLATFORM_UNCONFIRMED'

    def test_different_tracks_cannot_match_different_games(self):
        source=Source({'A.cue': {'crc': 'aa'}, 'B.bin': {'crc': 'bb'}})
        dat=Dat(mapping={'aa':(Match(),),'bb':(Match(game='Metroid Fusion'),)})
        assert self.evaluate(source=source,dats=dat,groups=[Group(('A.cue','B.bin'))]).decision.code == 'AMBIGUOUS_MATCH'

    def test_two_tracks_with_same_game_are_held_until_atomic_publish_supported(self):
        source=Source({'A.cue': {'crc': 'aa'}, 'B.bin': {'crc': 'bb'}})
        dat=Dat(mapping={'aa':(Match(),),'bb':(Match(),)}, required_roms=2)
        decision = self.evaluate(source=source,dats=dat,groups=[Group(('A.cue','B.bin'))]).decision
        assert decision.action == 'hold'
        assert decision.code == 'REVIEW_REQUIRED'
        assert source.calls == [('A.cue','.cue'),('B.bin','.bin')]

    def test_ambiguous_same_game_different_catalogs_held(self):
        dat=Dat(mapping={'abcd1234':(Match(),Match(dat='Other Catalog'))})
        assert self.evaluate(dats=dat,mapping={'Nintendo - SNES':'snes','Other Catalog':'snes'}).decision.code == 'AMBIGUOUS_MATCH'

    def test_duplicate_results_held(self):
        dat=Dat(mapping={'abcd1234':(Match(),Match())})
        assert self.evaluate(dats=dat).decision.code == 'AMBIGUOUS_MATCH'


    def test_single_verified_track_of_multitrack_dat_game_held(self):
        decision = self.evaluate(dats=Dat(required_roms=2)).decision
        assert (decision.action, decision.code) == ('hold', 'SOURCE_INCOMPLETE')

    def test_single_member_of_three_member_dat_game_held(self):
        assert self.evaluate(dats=Dat(required_roms=3)).decision.code == 'SOURCE_INCOMPLETE'

    def test_missing_dat_game_membership_api_held(self):
        class WithoutMembers:
            verified_candidates = lambda self, **kw: (Match(),)
            lookup = lambda self, **kw: Match(status='unknown')
        assert self.evaluate(dats=WithoutMembers()).decision.code == 'DAT_IDENTITY_MISSING'

    def test_ambiguous_dat_game_membership_held(self):
        assert self.evaluate(dats=Dat(required_roms=None)).decision.code == 'DAT_IDENTITY_MISSING'

    def test_zero_member_dat_game_held(self):
        assert self.evaluate(dats=Dat(required_roms=0)).decision.code == 'DAT_IDENTITY_MISSING'

    def test_boolean_member_count_does_not_authorize(self):
        assert self.evaluate(dats=Dat(required_roms=True)).decision.code == 'DAT_IDENTITY_MISSING'

    def test_extra_member_of_single_rom_dat_game_not_authorized(self):
        source=Source({'A.bin': {'crc':'aa'}, 'B.bin': {'crc':'bb'}})
        dat=Dat(mapping={'aa':(Match(),), 'bb':(Match(),)}, required_roms=1)
        assert self.evaluate(source=source,dats=dat,groups=[Group(('A.bin','B.bin'))]).decision.code == 'REVIEW_REQUIRED'

    def test_dat_member_lookup_exception_held(self):
        class CannotRead(Dat):
            def game_rom_count(self, match):
                raise KeyError('simulated')
        assert self.evaluate(dats=CannotRead()).decision.code == 'DAT_IDENTITY_MISSING'


if __name__ == '__main__':
    unittest.main()
