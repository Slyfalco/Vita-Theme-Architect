"""Generate golden files for the v3 (Rust) theme.xml reader/writer.

Runs the CURRENT app's own write_theme_xml / parse_theme_xml (main_window.py) on a
set of theme_data cases and stores the exact results under
crates/vta-core/tests/golden/, so the Rust port can be checked byte-for-byte.

    .venv-x64\\Scripts\\python.exe scripts\\v3_golden.py

Outputs per case <name>:
  write/<name>.json  - theme_data input
  write/<name>.xml   - write_theme_xml output (newlines normalised to LF; the Python
                       app writes in text mode, so Windows files carry CRLF)
  parse/<name>.xml   - a theme.xml input
  parse/<name>.json  - parse_theme_xml output
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from main_window import VitaThemeArchitect  # noqa: E402

OUT = os.path.join(ROOT, "crates", "vta-core", "tests", "golden")

ALL_ICONS = ['icon_web.png', 'icon_calendar.png', 'icon_photos.png', 'icon_email.png',
             'icon_friend.png', 'icon_cma.png', 'icon_message.png', 'icon_music.png',
             'icon_near.png', 'icon_parental.png', 'icon_party.png', 'icon_power.png',
             'icon_ps3link.png', 'icon_ps4link.png', 'icon_settings.png', 'icon_trophy.png',
             'icon_video.png']

FULL_ASSETS = {**{f'br{i}.png': f'br{i}.png' for i in range(1, 11)},
               **{f'tbr{i}.png': f'tbr{i}.png' for i in range(1, 11)},
               **{n: n for n in ALL_ICONS},
               'lockpaper.png': 'lockpaper.png', 'bgm.at9': 'BGM.at9',
               'preview_livearea.png': 'preview_livearea.png',
               'preview_lockscreen.png': 'preview_lockscreen.png',
               'preview_thumbnail.png': 'preview_thumbnail.png',
               'screen_other.png': 'screen_other.png', 'screen_current.png': 'screen_current.png',
               'noNotice.png': 'noNotice.png', 'newNotice.png': 'newNotice.png'}

FULL_COLORS = {'m_dateColor': 'ff112233', 'm_notifyBgColor': 'ff445566',
               'm_notifyBorderColor': 'ff778899', 'm_notifyFontColor': 'ffaabbcc',
               'm_barColor': 'ff000000', 'm_indicatorColor': 'ffffffff',
               'm_noticeFontColor': 'ff00ff00', 'm_noticeGlowColor': 'ffca0000',
               'm_fontColor': 'ff7bd0ff'}

WRITE_CASES = {
    "minimal": {'title': 'Minimal', 'author': 'Slyfalco', 'version': '01.00',
                'assets': {}, 'colors': {}, 'page_colors': {}, 'page_waves': {}},
    "defaults_only": {},
    "full": {'title': 'Corpse Party Blood Drive', 'author': 'Difad002 Slyfalco',
             'version': '02.10', 'assets': FULL_ASSETS, 'colors': FULL_COLORS,
             'page_colors': {f'br{i}_color': f'ff{i:02d}{i:02d}{i:02d}' for i in range(1, 11)},
             'page_waves': {f'br{i}_wave': str(i) for i in range(1, 11)}},
    "per_page_colors": {'title': 'Per page', 'author': 'A', 'version': '01.00',
                        'use_global_font_color': False,
                        'assets': {'br1.png': 'br1.png', 'br3.png': 'br3.png', 'tbr3.png': 'tbr3.png',
                                   'br7.png': 'br7.png'},
                        'colors': {'m_fontColor': 'ffabcdef'},
                        'page_colors': {'br1_color': 'ff010101', 'br7_color': 'ff070707'},
                        'page_waves': {'br3_wave': '5'}},
    "gaps_no_br1": {'title': 'Gaps', 'author': 'B', 'version': '01.00',
                    'assets': {'tbr1.png': 'custom_tbr1.png', 'br2.png': 'br2.png', 'br10.png': 'br10.png'},
                    'colors': {}, 'page_colors': {}, 'page_waves': {}},
    "special_chars": {'title': 'Tom & Jerry <"quoted"> \'apos\' > end', 'author': 'Ünïcødé 日本語 ✨',
                      'version': '01.00', 'assets': {'bgm.at9': 'my bgm & stuff.at9'},
                      'colors': {}, 'page_colors': {}, 'page_waves': {}},
    "empty_strings": {'title': '', 'author': '', 'version': '',
                      'assets': {'br1.png': '', 'bgm.at9': '', 'icon_web.png': ''},
                      'colors': {'m_barColor': ''}, 'page_colors': {}, 'page_waves': {}},
    "whitespace": {'title': '  spaced  title ', 'author': 'tab\there', 'version': '01.00',
                   'assets': {}, 'colors': {}, 'page_colors': {}, 'page_waves': {}},
    "some_icons": {'title': 'Icons', 'author': 'C', 'version': '01.00',
                   'assets': {'icon_power.png': 'icon_power.png', 'icon_web.png': 'web.png',
                              'noNotice.png': 'noNotice.png'},
                   'colors': {}, 'page_colors': {}, 'page_waves': {}},
}

# Hand-made theme.xml inputs for the reader (beyond round-tripping the writer output).
PARSE_EXTRA = {
    "six_digit_colors": '''<?xml version="1.0" encoding="utf-8"?>
<theme format-ver="01.00" package="0">
\t<HomeProperty>
\t\t<m_bgParam>
\t\t\t<BackgroundParam>
\t\t\t\t<m_imageFilePath>br1.png</m_imageFilePath>
\t\t\t\t<m_fontColor>#a1b2c3</m_fontColor>
\t\t\t\t<m_waveType>3</m_waveType>
\t\t\t</BackgroundParam>
\t\t\t<BackgroundParam>
\t\t\t\t<m_imageFilePath>br2.png</m_imageFilePath>
\t\t\t\t<m_fontColor>ff00ff00</m_fontColor>
\t\t\t</BackgroundParam>
\t\t</m_bgParam>
\t</HomeProperty>
\t<InfomationBarProperty>
\t\t<m_barColor>#123456</m_barColor>
\t\t<m_indicatorColor>abcdef</m_indicatorColor>
\t</InfomationBarProperty>
\t<InfomationProperty>
\t\t<m_title><m_default>Six</m_default></m_title>
\t\t<m_provider><m_default>Dev</m_default></m_provider>
\t\t<m_contentVer>01.00</m_contentVer>
\t</InfomationProperty>
</theme>''',
    "missing_info": '''<theme><HomeProperty/></theme>''',
    "malformed": '''<theme><HomeProperty></theme>''',
    "entities_cdata": '''<?xml version="1.0" encoding="utf-8"?>
<theme><InfomationProperty><m_title><m_default>A &amp; B &lt;C&gt; &#x65E5;</m_default></m_title>
<m_provider><m_default><![CDATA[raw <cdata> & stuff]]></m_default></m_provider>
<m_contentVer>  01.00  </m_contentVer></InfomationProperty>
<StartScreenProperty><m_filePath>lock.png</m_filePath><m_dateColor></m_dateColor></StartScreenProperty></theme>''',
    "no_declaration_crlf": '<theme>\r\n<InfomationProperty>\r\n<m_title><m_default>CRLF</m_default></m_title>\r\n</InfomationProperty>\r\n</theme>\r\n',
}


def main():
    for sub in ("write", "parse"):
        d = os.path.join(OUT, sub)
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d)
    tmp = tempfile.mkdtemp()
    try:
        for name, data in WRITE_CASES.items():
            assert VitaThemeArchitect.write_theme_xml(None, tmp, data) is True
            with open(os.path.join(tmp, 'theme.xml'), 'rb') as f:
                raw = f.read().replace(b'\r\n', b'\n')
            with open(os.path.join(OUT, 'write', name + '.json'), 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            with open(os.path.join(OUT, 'write', name + '.xml'), 'wb') as f:
                f.write(raw)
            # Every writer output is also a reader case.
            PARSE_EXTRA.setdefault('roundtrip_' + name, raw.decode('utf-8'))
        for name, text in PARSE_EXTRA.items():
            src = os.path.join(OUT, 'parse', name + '.xml')
            with open(src, 'wb') as f:
                f.write(text.encode('utf-8'))
            parsed = VitaThemeArchitect.parse_theme_xml(None, src)
            with open(os.path.join(OUT, 'parse', name + '.json'), 'w', encoding='utf-8') as f:
                json.dump(parsed, f, ensure_ascii=False, indent=1)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("golden files written to", OUT)


if __name__ == "__main__":
    main()
