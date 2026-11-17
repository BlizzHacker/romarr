"""Frozen real release titles prove video hints and final-extension preferences."""
import pytest
from romarr.profiles import ReleaseProfile
from romarr.platforms import by_slug
from romarr.selection import Release, judge


def points(profile, title, content_path=""):
    """Return only the new preference delta."""
    return sum(value for value, _ in profile.adjustments(title.lower(), "psx", content_path=content_path))


def test_frozen_real_titles_exact_ranking():
    profile = ReleaseProfile(video_standard="ntsc", formats={"psx": ["chd", "cue", "bin"]})
    titles = ["Castlevania - Symphony of the Night (Europe).bin",
              "Castlevania - Symphony of the Night (USA).cue",
              "Castlevania - Symphony of the Night (USA).chd",
              "Castlevania - Symphony of the Night (Japan).bin"]
    assert [points(profile, title) for title in titles] == [-20, 50, 60, 40]
    assert sorted(titles, key=lambda title: points(profile,title), reverse=True) == [titles[2],titles[1],titles[3],titles[0]]
    platform=by_slug("psx")
    ranked=[]
    for title in titles:
        release=Release(title=title,size=300*1024*1024,seeders=30,categories=(1000,),download_url="magnet:?xt=urn:btih:test",protocol="torrent")
        ranked.append(judge(release,"Castlevania Symphony of the Night",platform,profile=profile))
    assert sorted(range(4),key=lambda i:ranked[i].points,reverse=True)==[2,1,3,0]


@pytest.mark.parametrize("title", ["Paladin's Quest", "Japan World Cup", "CHD Collection.zip", "Game.chd.exe", "Game (USA, Europe)", "Game (World)"])
def test_words_unknown_and_mixed_tags_stay_neutral(title):
    profile=ReleaseProfile(video_standard="pal",video_strict=True,formats={"psx":["chd"]})
    assert points(profile,title)==0
    assert not profile.refusal(title.lower())


def test_strict_only_refuses_known_opposition_and_member_extension_wins():
    profile=ReleaseProfile(video_standard="ntsc",video_strict=True,formats={"psx":["chd","cue"]})
    assert profile.refusal("game (europe)")
    assert not profile.refusal("game ntsc-j")
    assert points(profile,"Game.zip","Game (USA).chd")==30
    assert points(profile,"Game.chd","Game.cue")==20


def test_platform_overrides_global_and_empty_formats_disable():
    settings={"video_standard":"ntsc","video_standard_strict":True,"preferred_formats":{"psx":[".CHD","cue","chd"]},"platform_profiles":{"psx":{"video_standard":"none","video_standard_strict":False,"preferred_formats":[]}}}
    profile=ReleaseProfile.from_settings(settings,by_slug("psx"))
    assert not profile.video_standard and not profile.video_strict and profile.formats["psx"]==[]
    global_profile=ReleaseProfile.from_settings(settings)
    assert global_profile.formats["psx"]==["chd","cue"]
