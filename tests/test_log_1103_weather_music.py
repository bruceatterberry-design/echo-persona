"""2026-11-03 (the owner's log): weather channel and "a different Steely Dan song"."""
import time

from core import browser, tools

A = ["steely dan", "dan fogelberg", "blues", "classic rock"]
RECENT = ("That's a funny mix-up, the owner. I think you meant Steely Dan, not Stilly Dance. Would you like me to play a "
          "different song by them instead, or shall we stick with Do It Again?")


def test_go_ahead_and_open_is_a_command():
    assert browser.parse("Go ahead and open the weather channel.").url == "https://weather.com/"
    a = browser.parse_play("Go ahead and open Steely Dan.", "owner", A)
    assert a and a.play == "steely dan"
    assert browser.is_yes("Go ahead")


def test_different_song_by_them_uses_the_artist_we_talked_about():
    a = browser.parse_play("Play a different song by them.", "owner", A, recent=RECENT)
    assert a.play == "steely dan" and a.random and a.exclude == []
    assert "them" not in a.say


def test_misheard_artist_and_instead_of():
    a = browser.parse_play("Sure, but instead of playing Do It Again, why don't you pick another Stilly Dance song?",
                           "owner", A)
    assert a.play == "steely dan" and a.avoid == ["Do It Again"]


def test_the_last_song_is_not_picked_again():
    last = {"artist": "steely dan", "vid": "aQnW-MxAU6U", "at": time.time()}
    a = browser.parse_play("play something else", "owner", A, last_play=last)
    assert a.play == "steely dan" and a.exclude == ["aQnW-MxAU6U"]


def test_them_without_anyone_to_refer_to_is_not_searched_literally():
    assert browser.parse_play("Play a different song by them.", "owner", A, recent="") is None
    assert browser.wants_action("Play a different song by them.", A)   # → the not-done note, no false "opening"


def test_song_titles_with_another_stay_as_said():
    assert browser.parse_play("play another brick in the wall", "owner", A) is None or \
        browser.parse_play("play another brick in the wall", "owner", A).play != "steely dan"


def test_offer_names_the_artist():
    o = browser.offer_from_reply('Rain is perfect for some Steely Dan. Shall I go open YouTube and play "Do It Again"?')
    assert o and o[0].play == "do it again steely dan"


def test_question_about_the_open_page_reads_it():
    last = {"host": "weather.com", "name": "The Weather Channel", "at": time.time(), "url": "https://weather.com/"}
    assert browser.page_to_read("So, what do you see on the weather channel?", last)[0] == "https://weather.com/"
    assert browser.page_to_read("What's the weather like?", None)[0] == "https://weather.com/"
    assert browser.page_to_read("what do you see", {**last, "at": time.time() - 7200}) is None
    assert browser.page_to_read("the weather is nice today", last) is None


def test_open_page_is_read_rendered(monkeypatch):
    from core import jsbrowser, web
    seen = []
    monkeypatch.setattr(jsbrowser, "read_page", lambda u, max_chars=0: seen.append("js") or web.Page(
        url=u, title="Weather", text="Now 64° Rain"))
    monkeypatch.setattr(jsbrowser, "read_smart", lambda u, max_chars=0: seen.append("smart") or web.Page(url=u, text="x"))
    t = tools.Turn()
    t.allow("https://weather.com/")
    t.live.add(tools._key("https://weather.com/"))
    out = tools.read_webpage({"url": "https://weather.com/", "question": "what do you see"}, t)
    assert seen == ["js"] and "64°" in out
