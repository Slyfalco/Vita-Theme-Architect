"""
VitaTheme Architect - AI icon generation worker.

Runs inside the private icon-AI Python runtime (see icon_generator.py), never
inside the app itself, so torch/diffusers stay out of the main build.

Usage:
    python -I icon_ai_worker.py <job.json>

The job file holds: title, templates_dir, wallpapers, icons, output_dir,
creativity, style_scale, consistency, clarity, layout_style, steps, seed,
and optionally examples_dirs (folders of example icon sets), cache_dir,
theme_colors and theme_color_mode ('auto', 'always' or 'never'), and
extra_images (user-provided character artwork: renders, official art).
Progress is reported on stdout as lines prefixed with EVENT_PREFIX followed by
a JSON object; anything else on stdout/stderr is plain log output (model
download progress, warnings).

How an icon is made:
  1. The template's symbol is extracted as a soft mask. Its text (www, 31,
     PS4) is left out and typeset afterwards with a real font.
  2. Characters are cut out of the theme's own wallpapers with an anime
     segmentation model, so they are exactly the theme's characters.
  3. Each icon gets a layout (character beside / behind the symbol, portrait,
     or symbol-only emblem), varied across the set. Layouts are also learned
     from example icon sets (built-in and downloaded community themes): the
     same segmentation finds where the example's character and symbol sit.
  4. A ControlNet follows the symbol and character outlines while the AI
     paints the background and restyles the symbol; IP-Adapter uses the
     wallpapers only as a style/colour reference and the AI is told not to
     draw characters of its own.
  5. The result is colour-matched to the theme's palette, the real character
     cut-out is placed back on top unchanged, and the symbol gets one accent
     colour for the whole set plus a dark outline so it stays readable.
"""
import json
import os
import random
import sys

EVENT_PREFIX = '@@VTA '

BASE_MODEL = 'stable-diffusion-v1-5/stable-diffusion-v1-5'
# Optional high-quality model (job['model'] == 'sdxl'): an anime-tuned SDXL
# (OpenRAIL++) with a small SDXL canny ControlNet and the SDXL style adapter,
# which reuses the ViT-H image encoder already downloaded for SD 1.5.
# About 8 GB more to download; fits in under 8 GB of VRAM at SDXL_SIZE.
SDXL_MODEL = 'cagliostrolab/animagine-xl-4.0'
SDXL_CONTROLNET = 'diffusers/controlnet-canny-sdxl-1.0-small'
SDXL_IP_WEIGHTS = 'ip-adapter_sdxl_vit-h.safetensors'
# AI poses with the SDXL model (optional, ~3.4 GB more): an SDXL OpenPose
# ControlNet and the SDXL 'plus' IP-Adapter that holds a character's look.
SDXL_OPENPOSE = 'xinsir/controlnet-openpose-sdxl-1.0'
SDXL_IP_PLUS_WEIGHTS = 'ip-adapter-plus_sdxl_vit-h.safetensors'
SDXL_SIZE = 768
# Animagine's quality tags, put FIRST: appended, they'd be cut off at 77 tokens.
SDXL_QUALITY = 'masterpiece, best quality'
SDXL_NEGATIVE = 'worst quality, low score, bad score'
CONTROLNET_MODEL = 'lllyasviel/control_v11p_sd15_canny'
IP_ADAPTER_REPO = 'h94/IP-Adapter'
# Style/colour only: characters come from real cut-outs, so the adapter must
# not invent lookalikes (the 'plus' variant copies characters too strongly).
IP_ADAPTER_WEIGHTS = 'ip-adapter_sd15.safetensors'
# Optional pose & expression variation (job['pose_variation']): reused
# characters are redrawn in a new pose from the pose library, guided by an
# OpenPose ControlNet, with the 'plus' adapter holding their look.
OPENPOSE_MODEL = 'lllyasviel/control_v11p_sd15_openpose'
IP_ADAPTER_PLUS_WEIGHTS = 'ip-adapter-plus_sd15.safetensors'
# --- FLUX.2 (Phase 2: the large-model 'reference' backend) -------------------
# A smarter model needs no ControlNet/IP-Adapter scaffolding: it's driven like
# the Gemini path — a prompt plus reference images (the symbol, wallpapers and
# icons already made). Shipped as the 4-bit build so it fits ~8-12 GB with
# group offloading. FLUX.2 weights are non-commercial; the user downloads them
# themselves (opt-in) and the icons they make are usable (see the app's accept).
FLUX2_REPO = 'black-forest-labs/FLUX.2-dev'       # VAE (bf16) + pipeline config/scheduler
FLUX2_BNB_REPO = 'diffusers/FLUX.2-dev-bnb-4bit'  # 4-bit transformer + Mistral3 text encoder
FLUX2_SIZE = 1024
FLUX2_STEPS = 28
FLUX2_GUIDANCE = 4.0        # single/low-reference
FLUX2_GUIDANCE_MULTI = 2.5  # with several reference images
FLUX2_MAX_REFS = 10         # FLUX.2 supports up to 10 reference images
FLUX2_MAX_WALLPAPERS = 5
FLUX2_MAX_EXTRAS = 2
FLUX2_REF_SIZE = 1024       # references sent at most this big
# Two FLUX.2 tiers (both optional): the 4-bit build runs on ~8-12 GB cards with
# group offloading (slower, smaller download); the full bf16 build is faster and
# higher quality on high-end GPUs (much larger download).
FLUX2_VARIANTS = {
    'flux2': {'quant': '4bit'},
    'flux2_full': {'quant': 'bf16'},
}

SEGMENT_REPO = 'skytnt/anime-seg'   # Apache-2.0, ISNet tuned for anime/cartoon art
SEGMENT_FILE = 'isnetis.onnx'
# Small text detector (PaddleOCR v4 det, Apache-2.0): finds lettering the AI
# invents, or that cut-outs carry over from wallpaper logos, so it can be removed.
TEXT_DET_REPO = 'SWHL/RapidOCR'
TEXT_DET_FILE = 'PP-OCRv4/ch_PP-OCRv4_det_infer.onnx'
TEXT_THRESHOLD = 0.3   # 0.2 mistook patterns for lettering on most icons
TEXT_REPAINT_STRENGTH = 0.6
TEXT_REPAINT_AREA = 0.002    # only real text areas get a model repaint; specks are left alone
TEXT_REPAINT_TRIES = 2
DEFAULT_STEPS = 20           # with DPM++ 2M Karras (see run)  # light regeneration used to replace invented lettering
# Anime head detector (deepghs, MIT): splits group shots into individual
# characters, rejects stray fragments (no head = not a character), and frames
# faces properly.
HEAD_REPO = 'deepghs/anime_head_detection'
HEAD_FILE = 'head_detect_v0.5_s_pruned/model.onnx'
HEAD_THRESHOLD = 0.472   # the model's published F1-optimal threshold
MIN_HEAD_PX = 48         # smaller heads turn to mush when enlarged into an icon
HEAD_REACH = 4.0         # a character's body reaches at most ~4 head-widths from its head
# Anime character identity model (deepghs CCIP, OpenRAIL): recognises the same
# character in different wallpapers, so a cut-off character can be swapped
# for a complete picture of the same person. Strict on purpose: casts in
# matching uniforms confuse it, and a wrong person is worse than no swap.
IDENTITY_REPO = 'deepghs/ccip_onnx'
IDENTITY_DIR = 'ccip-caformer-24-randaug-pruned'
# Art pulled automatically from community themes of the same series: a
# character from it is only used if it is clearly one of this theme's own.
COMMUNITY_ART_MATCH = 0.15
# ...and a picture counts as drawn in the theme's style when the style model
# gives the theme's style at least this score (single pictures of one series
# often split between e.g. "anime screenshot" and "illustration").
COMMUNITY_STYLE_MIN = 0.25
SAME_CHARACTER_DIFF = 0.10   # well under the model's own 0.178 threshold
SAME_CHARACTER_MARGIN = 0.03  # and clearly closer than the next candidate
SCENERY_BASE_MIX = 0.5        # how much of the theme's own scenery shapes a scenery background's start
SCENERY_STRENGTH = 0.72       # scenery backgrounds reinterpret the theme's world rather than invent a new one
# How strongly the theme's look (style adapter) steers each kind of background.
# Higher for backgrounds that were drifting away from the theme.
# (0.75 pulled the theme's characters into backgrounds as invented figures.)
STYLE_FOR = {'scenery': 0.65, 'character-scenery': 0.6, 'character-design': 0.6}
# Share of symbol-only icons set in the theme's environment ('scene' layout:
# a smaller symbol placed anywhere in a real piece of the theme's world). The
# rest get the designed emblem (the user's favourite look), which is built on
# the theme's art too: every icon shows the theme's art, never effects alone.
# Chosen by the seed.
SCENE_SHARE = 0.6
THEME_COLOR_AGREEMENT = 0.3   # share of the art's colourful pixels in the UI colours' hues for them to lead the palette
SCENE_BASE_MIX = 0.75         # how much of the real environment the scene icon starts from
SCENE_STRENGTH = 0.6          # redraws the environment in icon style but keeps the place recognisable
DESIGN_ART_MIX = 0.5          # how much of a real piece of the theme's art a designed emblem starts from
DESIGN_ART_STRENGTH = 0.78    # designed emblems redraw that art as ornament, but it must still show
SCENE_ZOOM = (0.6, 1.0)       # environment crop size as a share of the picture's short side
SCENE_MAX_FIGURES = 0.015     # most of a scene crop that may be character (pieces of bodies looked like leftovers; filling them out made messy scenes)
SCENE_MAX_TEXT = 0.001        # scene crops with logos or titles are skipped (the lettering came back garbled)
SCENE_CHECKS = 4              # least-character crops per picture checked with the detectors
# A scenery crop must have real content: character-free parts of character-
# heavy wallpapers are often blank gradients or blur (edge density ~0.02-0.035,
# Elfen Lied), which icons then filled with glow. Real scenery measured
# 0.06-0.22 (FNF Tricky, Corpse Party).
SCENE_MIN_DETAIL = 0.05
DESIGN_STRENGTH = 0.85        # designed emblems need room to build their ornament (the favourite's setting)
CHARACTER_BACKDROP_STRENGTH = 0.82  # enough for character icons to build a real backdrop (0.75 stayed near-plain)
# Designed emblems with no clean scenery crop start from the theme's softened
# art: from a plain gradient they came out as light effects and the icon
# only (0.18 earlier became random settings; 0 left nothing of the theme).
DESIGN_SCENERY_MIX = 0.45
DESIGN_SOFT_STRENGTH = 0.75   # ...redrawn less hard, or the theme art under it was painted over with glow
# Designed-emblem variety in the spirit of the user's favourite (radial, ornate,
# glowing). No words that tempt the model into drawing text (runes, banners...).
# Ornaments come from the theme's own art: generic patterns (sunbursts, star
# patterns, halo rings, crystal shards, mosaics) showed up as rays, rings and
# stars with nothing to do with the theme.
EMBLEM_ORNAMENTS = ['motifs from the reference art', 'shapes from the reference art', 'filigree like the reference art',
                    'ornament like the reference art', 'fine filigree']
EMBLEM_LIGHTS = ['light radiating from the symbol', 'a backlit glow behind the symbol',
                 'soft rim light around the symbol', 'light falling from above']
EMBLEM_FRAMES = ['round badge', 'shield', 'hexagonal badge', 'diamond badge', 'open frameless']
EMBLEM_FINISHES = ['enamel', 'polished metal', 'crystal', 'painted', 'carved']
# Redraw (job['redraw'], default on): after the background is painted, the
# real character is placed and the icon is repainted lightly so the character
# is drawn into it (style, light, edges) instead of pasted, and cut characters
# get their cut continued. Kept only if the identity model still matches.
REDRAW_STRENGTH = 0.45         # the face is restored afterwards, so the body can really be redrawn
# The symbol is drawn by the AI as its own artwork (plain dark background, so
# it lifts out cleanly), loosely guided by the design shape so its silhouette
# can really change; kept if its outline still reads as the app's symbol.
SYMBOL_ART_STRENGTH = 0.85
SYMBOL_ART_GUIDE = 0.55        # ControlNet weight on the design shape
SYMBOL_ART_GUIDE_END = 0.6     # ...released for the last 40% of steps
SYMBOL_ART_SHAPE = 0.4         # overlap of the drawing's core (aligned) with the design shape to count as the same symbol
SYMBOL_ART_AREA = (0.5, 1.9)   # drawn area relative to the design shape
SYMBOL_ART_TRIES = 2           # a rejected drawing gets one more go (new seed) before the design shape is used
# Symbols made of several separate small pictures (the content manager's
# note, film, camera and controller) blurred into splotches when redrawn as
# one artwork. They are redrawn more gently, follow their outline harder and
# longer, and every part must still be there.
MULTIPART_ICONS = {'icon_cma.png'}   # only these: every other icon draws as before
MULTIPART_MIN_PARTS = 3
MULTIPART_ART_STRENGTH = 0.65
MULTIPART_ART_GUIDE = 0.85
MULTIPART_ART_GUIDE_END = 0.9
MULTIPART_PART_COVER = 0.5     # share of each part the drawing must still cover
SYMBOL_ART_DETAIL = 45         # brightness gap a drawing needs between its design's inner features and the body around them
# Symbol edges: alpha below EDGE_CUT (the soft glow fringe) is dropped and the
# rest stretched into a short crisp edge (the fringe read as a blurry halo at icon size).
SYMBOL_EDGE_CUT = 90
SYMBOL_EDGE_TOP = 40
SYMBOL_ART_STEPS = 12          # speed: DPM++ handles simple shapes well in few steps
REPAIR_STRENGTH = 0.55
REDRAW_MATCH = 0.12            # CCIP difference of the whole figure: at or under means the same character
# The face is never redrawn: the original is blended back in after the redraw.
FACE_KEEP_SCALE = 1.2          # restored area around the detected head
# Anime face detector (deepghs, MIT): front/side views beat back-of-head shots
# when choosing which picture of a character to use.
FACE_REPO = 'deepghs/anime_face_detection'
FACE_FILE = 'face_detect_v1.4_n/model.onnx'
FACE_THRESHOLD = 0.278          # the model's published F1-optimal threshold
# Anime person detector (deepghs, MIT): catches invented bodies without a
# visible face (e.g. a headless figure), which the head detector misses.
PERSON_REPO = 'deepghs/anime_person_detection'
PERSON_FILE = 'person_detect_v1.3_s/model.onnx'
# The theme's rendering style (3D, anime screenshot, comic, illustration, real
# photo) so the icons are drawn the way the theme is: a realistic theme gets
# realistic icons, a cartoony one cartoony icons. 17 MB.
# What is actually in the theme's wallpapers (scenery and objects: "classroom",
# "blood", "sky", "sword"), from an anime image tagger (~0.4 GB, CPU). Named in
# the prompts of symbol-only icons, so their backgrounds show the theme's own
# things instead of only its colours.
TAGGER_REPO = 'SmilingWolf/wd-vit-tagger-v3'
TAGGER_THRESHOLD = 0.35
THEME_MOTIFS = 6               # most common scenery/object tags kept
# Tags about people, bodies, clothing, hairstyles, poses and picture framing:
# not the theme's world (substring match).
NOT_MOTIFS = (
    'girl', 'boy', 'hair', 'eye', 'breast', 'smile', 'mouth', 'looking', 'holding', 'dress', 'shirt', 'skirt',
    'sleeve', 'solo', 'multiple', 'bangs', 'ears', 'tail', 'hand', 'arm', 'leg', 'thigh', 'skin', 'lips', 'blush',
    'teeth', 'tongue', 'standing', 'sitting', 'upper body', 'cowboy shot', 'full body', 'bare', 'collar', 'glove',
    'boot', 'shoe', 'footwear', 'heels', 'navel', 'cleavage', 'shoulder', 'neck', 'nose', 'parted', 'closed', 'open ',
    'jacket', 'uniform', 'serafuku', 'gakuran', 'pantyhose', 'socks', 'horns', 'facing', 'profile', 'from ',
    'expression', 'fang', 'tears', 'crying', 'facial', 'male', 'female', 'people', 'person', 'character', 'portrait',
    'pose', 'feet', 'finger', 'nail', 'sweat', 'ahoge', 'ponytail', 'twintails', 'braid', 'drills', 'headgear', 'hood',
    'cape', 'coat', 'pants', 'shorts', 'necktie', 'bowtie', 'choker', 'earrings', 'jewelry', 'hairband', 'hairclip',
    'ornament', 'medium', 'long', 'short', 'large', 'small', 'text', 'logo', 'watermark', 'signature', 'artist',
    'copyright', 'background', 'monochrome', 'greyscale', 'border', 'letterbox', 'no humans', 'official', 'cover',
    'game cg', 'coloring', 'chibi', 'censored', 'parody', 'glasses', 'gauntlet', 'cap', 'hat', 'bandana', 'pointing',
    'ribbon', 'armor', 'headphones', 'mask', 'scarf', 'apron', 'vest', 'belt', 'bag', 'wrist', 'cosplay', 'blurry',
    'depth of field', 'dutch angle', 'theme', 'cropped', 'pov', 'gradient', 'simple', 'cardigan', 'sweater',
    'carrying', 'spot color', 'wings',
)
# Iconic features (like a theme's mascot or a character's horns built into a
# symbol, without showing the character): the theme's recurring characters (by
# name: the SDXL model knows anime characters by their tags) and distinctive
# accessories, from the same tagger.
ICONIC_ITEMS = ('horns', 'hair ornament', 'hairclip', 'hair flower', 'ribbon', 'hat', 'mask', 'crown', 'halo', 'tiara',
                'animal ears', 'cat ears', 'fox ears', 'bunny ears', 'wings', 'eyepatch', 'scarf', 'headphones', 'bandana',
                'microphone', 'sword', 'scythe', 'katana', 'gun', 'staff', 'wand', 'bear', 'skull', 'teddy bear', 'cape')
ICONIC_MIN_COUNT = 2           # a trait must recur in at least this many wallpapers (one-off tags were wrong guesses)
# How each kind of item is added to a symbol, worded so the model draws it
# clearly (a vague "with X features" came out invisible).
ICONIC_WORN = ('horns', 'hair ornament', 'hairclip', 'hair flower', 'ribbon', 'hat', 'crown', 'halo', 'tiara',
               'animal ears', 'cat ears', 'fox ears', 'bunny ears', 'headphones', 'bandana')
ICONIC_HELD = ('sword', 'scythe', 'katana', 'gun', 'staff', 'wand', 'microphone')
# Items with a silhouette (item_silhouette); others can't be drawn reliably.
ICONIC_DRAWABLE = ('horns', 'crown', 'tiara', 'halo', 'animal ears', 'cat ears', 'fox ears', 'bunny ears', 'hat',
                   'headphones', 'ribbon', 'hair ornament', 'hairclip', 'hair flower', 'sword', 'katana', 'staff',
                   'wand', 'scythe', 'microphone', 'wings')
ICONIC_GUIDE, ICONIC_GUIDE_END, ICONIC_STRENGTH = 0.4, 0.45, 0.9   # looser outline guide: room to add the item
ICONIC_SHARE = 0.7             # share of drawn symbols that get one of the theme's iconic features (seeded)
# Recognisability for iconic symbols: features may be added around the symbol
# (more area allowed), but most of the symbol itself must still be drawn.
# The whole-outline match is not used for them: the added features change the
# outline, so a drawing that fully showed the symbol was still rejected.
ICONIC_AREA_MAX = 3.0
ICONIC_COVER = 0.65            # share of the symbol's own shape the drawing must still cover
ART_STYLE_REPO = 'deepghs/anime_classification'
ART_STYLE_FILE = 'mobilenetv3_v1.5_dist/model.onnx'
ART_STYLE_LABELS = ['3d', 'bangumi', 'comic', 'illustration', 'not_painting']
# label -> (prompt words that replace the old one-size "stylized", words to avoid)
ART_STYLES = {
    '3d': ('3d render, cgi', 'flat, 2d, sketch'),
    'bangumi': ('anime screenshot style, cel shading', '3d render, photo'),
    'comic': ('comic book style, inked lines', '3d render, photo'),
    'illustration': ('stylized digital illustration', '3d render, photo'),
    'not_painting': ('realistic, photorealistic', 'anime, cartoon, flat, drawing'),
}
PERSON_THRESHOLD = 0.324        # the model's published F1-optimal threshold
CHARACTER_BACKDROP_DESIGN = 0.25  # character icons: share with a designed backdrop; the rest get theme scenery, which suited characters far better
SEAM_TOLERANCE = 3            # seam cells inside a framing that count as a visible diagonal cut
MAX_FACE_COVERED = 0.15       # how much of a behind-the-symbol character's face the symbol may cover
MAX_USES_PER_PERSON = 3       # each reuse gets a different framing (or a new pose, if enabled)
POSE_IDENTITY_DIFF = 0.15     # a redraw whose face drifted further from the character is discarded
INVENTED_FIGURE_RETRIES = 2   # regenerations when the model paints people of its own
MIN_INVENTED_HEAD_PX = 20     # smaller "heads" at 512px are texture, not figures (scaled with GEN_SIZE)
SEGMENT_SIZE = 1024
GEN_SIZE = 512
ICON_SIZE = 128
MAX_STYLE_REFERENCES = 10
MAX_CHARACTERS = 16
MAX_CHARACTERS_WITH_EXTRAS = 32  # user-provided artwork adds more characters/poses
# Example icons often hide part of the symbol behind the character, so the
# measured size runs small; below this the symbol stops reading at 128px.
MIN_SYMBOL_SCALE = 0.66
MAX_SYMBOL_COVERED = 0.2   # a character covering more than this goes behind the symbol

# What each icon is, named by its app so the model knows what it's drawing
# (e.g. "power button", not just "circle with a vertical line"). Lettering the
# model might attempt is removed by the text detector and repaint.
ICON_SUBJECTS = {
    'icon_web.png': 'web browser globe',
    'icon_calendar.png': 'calendar',
    'icon_photos.png': 'photo camera',
    'icon_email.png': 'email envelope',
    'icon_friend.png': 'friends list faces',
    'icon_cma.png': 'content manager files',
    'icon_message.png': 'chat message bubbles',
    'icon_music.png': 'music note',
    'icon_near.png': 'location radar',
    'icon_parental.png': 'parental controls',  # no object named: designs differ (the template's grid, a wrench, a lock)
    'icon_party.png': 'voice chat headset',
    'icon_power.png': 'power button',
    'icon_ps3link.png': 'remote play handheld console',
    'icon_ps4link.png': 'remote play game controller',
    'icon_settings.png': 'settings toolbox',
    'icon_trophy.png': 'trophy cup',
    'icon_video.png': 'video play button',
}

# Template areas containing text (128px template coordinates). The AI garbles
# text, so these areas are left out of the AI's guide and the text is typeset
# afterwards with a real font:
#   'label' - accent text with a dark outline on a soft dark backing
#   'panel' - dark text on an accent-coloured panel (the calendar page)
TEXT_REGIONS = {
    'icon_web.png': ((18, 55, 110, 76), 'label', 'www'),
    'icon_calendar.png': ((34, 32, 96, 63), 'panel', '31'),
    'icon_ps4link.png': ((44, 98, 86, 118), 'label', 'PS4'),
}
MIN_SYMBOL_CONTRAST = 4.5  # WCAG contrast ratio for readable text

# Layouts, as fractions of the icon:
#   symbol = (scale, x offset, y offset) of the template symbol
#   character = (height, centre x, bottom y) of the cut-out, or None
#   front = character drawn over the symbol (False = peeking from behind)
#   dim = darken the character (portraits sit behind the symbol)
LAYOUTS = {
    'peek': dict(symbol=(0.70, 0.12, 0.08), character=(0.80, 0.30, 1.00), front=False),
    'lean': dict(symbol=(0.68, -0.13, 0.00), character=(0.86, 0.74, 1.02), front=True),
    'sit': dict(symbol=(0.64, 0.00, 0.15), character=(0.48, 0.50, 0.44), front=True),
    'bust': dict(symbol=(0.66, -0.14, -0.10), character=(0.66, 0.74, 1.02), front=True),
    'portrait': dict(symbol=(0.82, 0.00, 0.00), character=(1.10, 0.50, 1.06), front=False, dim=0.2),
    'emblem': dict(symbol=(1.00, 0.00, 0.00), character=None, front=False, emblem=True),
    'scene': dict(symbol=(0.72, 0.00, 0.00), character=None, front=False, scene=True),
}
# Layout styles offered in the dialog; 'mixed' varies across the whole set
# and also draws on example layouts; 'examples' prefers example layouts.
LAYOUT_STYLES = {
    'mixed': ['peek', 'lean', 'sit', 'bust', 'portrait', 'emblem'],
    'examples': ['peek', 'lean', 'sit', 'bust', 'portrait', 'emblem'],
    'beside': ['peek', 'lean', 'sit', 'bust'],
    'portrait': ['portrait'],
    'emblem': ['emblem'],
}
EXAMPLE_CHANCE = {'mixed': 0.5, 'examples': 1.0}  # chance an example layout is used when one exists
LAYOUT_CACHE_VERSION = 3


# --- Model backends ---------------------------------------------------------
# A backend describes how a model is loaded and which generation recipe drives
# it. SD 1.5 / SDXL use the 'scaffold' recipe: ControlNet + IP-Adapter + real
# character cut-outs + an optional pose redraw, all needed because small models
# can't stay faithful on their own. Larger, smarter models (Phase 2, e.g.
# FLUX.2) will register here with recipe 'reference' and a light prompt+
# reference-image path (like the Gemini one), so they aren't held back by the
# small-model scaffolding. 'base_repo' is the checkpoint a custom model replaces.
BACKENDS = {
    'sd15': {'label': 'Standard (SD 1.5)', 'arch': 'sd15', 'recipe': 'scaffold', 'base_repo': BASE_MODEL},
    'sdxl': {'label': 'High quality (SDXL)', 'arch': 'sdxl', 'recipe': 'scaffold', 'base_repo': SDXL_MODEL},
    'flux2': {'label': 'Ultra (FLUX.2 4-bit, local)', 'arch': 'flux2', 'recipe': 'reference', 'base_repo': FLUX2_REPO},
    'flux2_full': {'label': 'Ultra+ (FLUX.2 full, local)', 'arch': 'flux2', 'recipe': 'reference', 'base_repo': FLUX2_REPO},
}


def backend_for(job):
    return BACKENDS.get(job.get('model', 'sd15'), BACKENDS['sd15'])


def detect_base_arch(path):
    """Best-effort guess of 'sd15' or 'sdxl' for a user-supplied checkpoint
    (a single .safetensors file or a diffusers-format folder), so the matching
    ControlNet/IP-Adapter are loaded. The user can override this in Settings.
    Defaults to 'sdxl' when unsure (most modern custom checkpoints are SDXL)."""
    try:
        if os.path.isdir(path):
            return 'sdxl' if os.path.isdir(os.path.join(path, 'text_encoder_2')) else 'sd15'
        import struct
        with open(path, 'rb') as f:
            header = json.loads(f.read(struct.unpack('<Q', f.read(8))[0]))
        keys = list(header.keys())
        # SDXL has a second text encoder and a label/added-conditioning embedding.
        if any(k.startswith('conditioner.embedders.1') or 'label_emb' in k or 'add_embedding' in k for k in keys):
            return 'sdxl'
        if any(k.startswith('cond_stage_model.transformer') or k.startswith('text_model.') for k in keys):
            return 'sd15'
        return 'sdxl'
    except Exception:
        return 'sdxl'


def inspect_custom_model(path, arch='sd15'):
    """Read extra properties of a user's checkpoint so it's used correctly:
    - 'inpaint': an inpainting model (9-channel UNet) this generator can't use;
    - 'prediction_type'/'zsnr': v-prediction / zero-terminal-SNR (some anime SDXL
      models need these or come out washed-out);
    - 'native_res': the resolution it was trained for;
    - 'domain': 'anime' or 'realistic', to match the prompt wording.
    Best-effort (reads the safetensors header / diffusers configs only)."""
    info = {'inpaint': False, 'prediction_type': None, 'zsnr': False, 'native_res': None, 'domain': None}
    try:
        if os.path.isdir(path):
            try:
                with open(os.path.join(path, 'unet', 'config.json'), encoding='utf-8') as f:
                    if int(json.load(f).get('in_channels', 4)) >= 9:
                        info['inpaint'] = True
            except (OSError, ValueError):
                pass
            try:
                with open(os.path.join(path, 'scheduler', 'scheduler_config.json'), encoding='utf-8') as f:
                    sc = json.load(f)
                if sc.get('prediction_type') == 'v_prediction':
                    info['prediction_type'] = 'v_prediction'
                if sc.get('rescale_betas_zero_snr'):
                    info['zsnr'] = True
            except (OSError, ValueError):
                pass
            return info
        import struct
        with open(path, 'rb') as f:
            header = json.loads(f.read(struct.unpack('<Q', f.read(8))[0]))
        meta = header.get('__metadata__', {}) or {}
        # Inpaint: the UNet's first conv has 9 input channels (4 for normal models).
        for key in ('model.diffusion_model.input_blocks.0.0.weight',
                    'model.diffusion_model.img_in.weight'):
            shape = (header.get(key) or {}).get('shape')
            if shape and len(shape) >= 2 and shape[1] >= 9:
                info['inpaint'] = True
        if str(meta.get('modelspec.prediction_type', '')).lower().startswith('v'):
            info['prediction_type'] = 'v_prediction'
        if str(meta.get('ztsnr', meta.get('modelspec.ztsnr', ''))).lower() in ('true', '1', 'yes'):
            info['zsnr'] = True
        try:
            res = str(meta.get('modelspec.resolution', '')).replace('x', ',')
            info['native_res'] = int(float(res.split(',')[0].strip()))
        except (ValueError, IndexError):
            pass
        blob = ' '.join(str(meta.get(k, '')) for k in
                        ('modelspec.title', 'modelspec.tags', 'modelspec.architecture', 'ss_tag_frequency')).lower()
        if any(w in blob for w in ('anime', 'pony', 'illustrious', 'danbooru', 'booru', 'cartoon', 'toon')):
            info['domain'] = 'anime'
        elif any(w in blob for w in ('realis', 'photoreal', 'photo-real', 'photograph')):
            info['domain'] = 'realistic'
    except Exception:
        pass
    return info


def _load_base(cls, custom_base, **kw):
    """Load a pipeline from a user's own checkpoint: a single .safetensors file
    via from_single_file, or a diffusers-format folder via from_pretrained.
    Loaded in place — nothing is copied, so a model shared with another app
    costs no extra disk."""
    if os.path.isdir(custom_base):
        return cls.from_pretrained(custom_base, **kw)
    return cls.from_single_file(custom_base, **kw)


def model_files(half, segmenter=True, poses=False, model='sd15', skip_base=False):
    """Every model file a generation needs, as (repo, path). Shared with the
    app (loaded from this file) for the download-size estimate. With skip_base,
    the base checkpoint's files are dropped (a custom model supplies them)."""
    v = '.fp16' if half else ''
    if model == 'sdxl':
        files = [(SDXL_MODEL, p) for p in (
            'model_index.json', 'scheduler/scheduler_config.json',
            'tokenizer/merges.txt', 'tokenizer/special_tokens_map.json', 'tokenizer/tokenizer_config.json', 'tokenizer/vocab.json',
            'tokenizer_2/merges.txt', 'tokenizer_2/special_tokens_map.json', 'tokenizer_2/tokenizer_config.json', 'tokenizer_2/vocab.json',
            'text_encoder/config.json', 'text_encoder/model.safetensors',
            'text_encoder_2/config.json', 'text_encoder_2/model.safetensors',
            'unet/config.json', 'unet/diffusion_pytorch_model.safetensors',
            'vae/config.json', 'vae/diffusion_pytorch_model.safetensors')]
        files += [(SDXL_CONTROLNET, 'config.json'), (SDXL_CONTROLNET, f'diffusion_pytorch_model{v}.safetensors')]
        files += [(IP_ADAPTER_REPO, f'sdxl_models/{SDXL_IP_WEIGHTS}'), (IP_ADAPTER_REPO, 'models/image_encoder/config.json'),
                  (IP_ADAPTER_REPO, 'models/image_encoder/model.safetensors')]
        if poses:
            files += [(SDXL_OPENPOSE, 'config.json'), (SDXL_OPENPOSE, 'diffusion_pytorch_model.safetensors'),
                      (IP_ADAPTER_REPO, f'sdxl_models/{SDXL_IP_PLUS_WEIGHTS}')]
        base_repo = SDXL_MODEL
    else:
        files = [(BASE_MODEL, p) for p in (
            'model_index.json', 'scheduler/scheduler_config.json', 'feature_extractor/preprocessor_config.json',
            'tokenizer/merges.txt', 'tokenizer/special_tokens_map.json', 'tokenizer/tokenizer_config.json',
            'tokenizer/vocab.json', 'text_encoder/config.json', f'text_encoder/model{v}.safetensors',
            'unet/config.json', f'unet/diffusion_pytorch_model{v}.safetensors',
            'vae/config.json', f'vae/diffusion_pytorch_model{v}.safetensors')]
        files += [(CONTROLNET_MODEL, 'config.json'), (CONTROLNET_MODEL, f'diffusion_pytorch_model{v}.safetensors')]
        files += [(IP_ADAPTER_REPO, f'models/{IP_ADAPTER_WEIGHTS}'), (IP_ADAPTER_REPO, 'models/image_encoder/config.json'),
                  (IP_ADAPTER_REPO, 'models/image_encoder/model.safetensors')]
        if poses:
            files += [(OPENPOSE_MODEL, 'config.json'), (OPENPOSE_MODEL, f'diffusion_pytorch_model{v}.safetensors'),
                      (IP_ADAPTER_REPO, f'models/{IP_ADAPTER_PLUS_WEIGHTS}')]
        base_repo = BASE_MODEL
    if skip_base:
        files = [(r, p) for r, p in files if r != base_repo]
    return files + _helper_model_files(segmenter)


def _helper_model_files(segmenter):
    """Small detector/identity models used by both image models."""
    files = []
    if segmenter:
        files.append((SEGMENT_REPO, SEGMENT_FILE))
    files.append((TEXT_DET_REPO, TEXT_DET_FILE))
    files.append((HEAD_REPO, HEAD_FILE))
    files.append((FACE_REPO, FACE_FILE))
    files.append((PERSON_REPO, PERSON_FILE))
    files.append((ART_STYLE_REPO, ART_STYLE_FILE))
    files += [(TAGGER_REPO, 'model.onnx'), (TAGGER_REPO, 'selected_tags.csv')]
    files += [(IDENTITY_REPO, f'{IDENTITY_DIR}/model_feat.onnx'), (IDENTITY_REPO, f'{IDENTITY_DIR}/model_metrics.onnx')]
    return files


def ensure_models(half, segmenter, poses=False, model='sd15', skip_base=False):
    """Download missing model files first, reporting bytes, speed and time left."""
    import threading
    import time
    from huggingface_hub import HfApi, hf_hub_download, try_to_load_from_cache
    missing = [(r, p) for r, p in model_files(half, segmenter, poses, model, skip_base=skip_base) if not isinstance(try_to_load_from_cache(r, p), str)]
    if not missing:
        return
    api = HfApi()
    sizes = {}
    for repo in {r for r, _p in missing}:
        for info in api.get_paths_info(repo, [p for r, p in missing if r == repo]):
            sizes[(repo, info.path)] = getattr(info, 'size', 0) or 0
    total = sum(sizes.values()) or 1
    hub_dir = os.path.join(os.environ.get('HF_HOME', os.path.expanduser('~/.cache/huggingface')), 'hub')

    def cache_bytes():
        n = 0
        for root, _dirs, files in os.walk(hub_dir):
            for f in files:
                try:
                    n += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        return n

    start_bytes, start_time = cache_bytes(), time.time()
    errors = []

    def fetch():
        try:
            for repo, path in missing:
                hf_hub_download(repo, path)
        except Exception as e:  # reported after the progress loop
            errors.append(e)

    worker = threading.Thread(target=fetch, daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(0.5)
        done = min(total, max(0, cache_bytes() - start_bytes))
        elapsed = time.time() - start_time
        speed = done / elapsed if elapsed > 1 else 0
        emit('download', done=done, total=total, speed=speed,
             eta=(total - done) / speed if speed > 0 else None)
    if errors:
        raise errors[0]
    emit('download', done=total, total=total, speed=0, eta=0)


def emit(event, **data):
    print(EVENT_PREFIX + json.dumps({'event': event, **data}), flush=True)


def _peak_ram_gb():
    """Peak resident memory of this worker process, in GB (0 if unknown). In
    minimal-VRAM mode the offloaded model weights live in system RAM, so this
    is what the calibration data uses to record real RAM use."""
    try:
        if sys.platform == 'win32':
            import ctypes
            from ctypes import wintypes

            class _PMC(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                            ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                            ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                            ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                            ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]
            # Set types explicitly: the current-process pseudo-handle (-1) is
            # truncated without a HANDLE restype, and the call then fails.
            ctypes.windll.kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            get_mem = ctypes.windll.psapi.GetProcessMemoryInfo
            get_mem.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PMC), wintypes.DWORD]
            get_mem.restype = wintypes.BOOL
            counters = _PMC(); counters.cb = ctypes.sizeof(_PMC)
            if get_mem(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return counters.PeakWorkingSetSize / 1024 ** 3
        else:
            import resource
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS reports bytes; Linux reports kilobytes.
            return peak / 1024 ** 3 if sys.platform == 'darwin' else peak / 1024 ** 2
    except (OSError, ImportError, AttributeError, ValueError):
        pass
    return 0.0


def emit_peak(vram_bytes, tag):
    """Report the run's measured peak VRAM and system RAM, for calibration."""
    emit('peak', vram_gb=round((vram_bytes or 0) / 1024 ** 3, 2),
         ram_gb=round(_peak_ram_gb(), 2), setup=tag)


def pick_device():
    import torch
    if torch.cuda.is_available():
        return 'cuda', torch.float16
    if getattr(torch.backends, 'mps', None) and torch.backends.mps.is_available():
        return 'mps', torch.float32
    # float16 is unsupported/very slow for most CPU ops.
    return 'cpu', torch.float32


def load_rgb(path, size=None):
    from PIL import Image
    with Image.open(path) as img:
        img = img.convert('RGBA')
        flat = Image.new('RGB', img.size, (0, 0, 0))
        flat.paste(img, mask=img.getchannel('A'))
        return flat.resize(size, Image.Resampling.LANCZOS) if size else flat


# --- Theme palette -------------------------------------------------------------
def palette_colors(paths, count=5):
    """Dominant colours of the whole wallpaper set, most common first."""
    from PIL import Image
    pool = Image.new('RGB', (160, 90 * len(paths)))
    for i, p in enumerate(paths):
        pool.paste(load_rgb(p, (160, 90)), (0, 90 * i))
    quant = pool.quantize(colors=count, method=Image.Quantize.MEDIANCUT)
    pal = quant.getpalette()[:count * 3]
    counts = sorted(quant.getcolors(), reverse=True)
    return [tuple(pal[idx * 3:idx * 3 + 3]) for _n, idx in counts]


def theme_colors_in_art(theme_colors, paths, hue_reach=0.07):
    """Share of the wallpapers' colourful pixels whose hue matches one of the
    theme's UI colours: high when the UI colours come from the art (a red
    theme with red menus), low when they don't (an orange bar on blue art)."""
    import numpy as np
    from PIL import Image
    if not theme_colors or not paths:
        return 0.0
    pool = Image.new('RGB', (160, 90 * len(paths)))
    for i, p in enumerate(paths):
        pool.paste(load_rgb(p, (160, 90)), (0, 90 * i))
    hsv = np.asarray(pool.convert('HSV'), dtype=np.float32) / 255
    vivid = (hsv[..., 1] >= 0.25) & (hsv[..., 2] >= 0.25)
    if not vivid.any():
        return 0.0
    hues = hsv[..., 0][vivid]
    near = np.zeros(hues.shape, dtype=bool)
    for c in theme_colors:
        d = np.abs(hues - _hsv(c)[0])
        near |= np.minimum(d, 1 - d) <= hue_reach
    return float(near.mean())


def _hsv(c):
    import colorsys
    return colorsys.rgb_to_hsv(*(v / 255 for v in c))


def _luminance(c):
    lin = [(v / 255) / 12.92 if v / 255 <= 0.04045 else ((v / 255 + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def symbol_color(colors, background):
    """One colour for every symbol in the set, chosen against the theme's
    backgrounds: the theme's colour if it stands out, else that colour made
    lighter (dark backgrounds) or darker (light backgrounds), else white or
    black, whichever contrasts with the backgrounds."""
    import colorsys
    h, s, v = max((_hsv(c) for c in colors), key=lambda hsv: hsv[1] * hsv[2])
    if s < 0.25:
        accent = (245, 245, 245)
    else:
        r, g, b = colorsys.hsv_to_rgb(h, min(s, 0.75), 1.0)
        accent = (int(r * 255), int(g * 255), int(b * 255))
    target = 0 if _luminance(background) > 0.35 else 255  # light backgrounds: go darker
    for step in range(11):
        t = step / 10
        candidate = tuple(int(c + (target - c) * t) for c in accent)
        if _contrast(candidate, background) >= MIN_SYMBOL_CONTRAST:
            return candidate
    return (target,) * 3


def recolor_symbol(art, color):
    """A drawn symbol in the set's one colour. The AI gave every symbol its
    own colours and colour noise (a glitchy look, different on every icon);
    its shading (bevel, depth, detail) is kept as darker and lighter tones
    of the set colour, with the noise smoothed out."""
    import numpy as np
    from PIL import Image, ImageFilter
    alpha = art.getchannel('A')
    lum = np.asarray(art.convert('L').filter(ImageFilter.MedianFilter(5)), dtype=np.float32)
    solid = np.asarray(alpha) > 64
    lo, hi = np.percentile(lum[solid], [5, 95]) if solid.any() else (0.0, 255.0)
    t = np.clip((lum - lo) / max(1.0, hi - lo), 0, 1)[..., None]
    c = np.array(color, dtype=np.float32)
    dark, light = c * 0.45, c + (255 - c) * 0.55
    rgb = np.where(t < 0.5, dark + (c - dark) * (t / 0.5), c + (light - c) * ((t - 0.5) / 0.5))
    return Image.merge('RGBA', (*Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), 'RGB').split(), alpha))


def color_name(c):
    h, s, v = _hsv(c)
    if s < 0.2:
        return 'white'
    names = [(0.04, 'red'), (0.11, 'orange'), (0.18, 'yellow'), (0.45, 'green'), (0.55, 'cyan'),
             (0.70, 'blue'), (0.80, 'purple'), (0.95, 'pink'), (1.01, 'red')]
    return next(name for limit, name in names if h < limit)


def gradient_base(colors, index, size, light=(0.5, 0.5), rim=None):
    """Background lit from the symbol: a theme-colour glow centred on the
    symbol fading to a darker theme colour, plus an optional softer rim light
    behind the character, so the AI's art is arranged around the layout."""
    import numpy as np
    from PIL import Image
    lum = lambda c: 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]
    # Keep the background darker than the symbol so it stays readable.
    usable = [c for c in colors if lum(c) < 170] or [tuple(int(v * 0.5) for v in colors[0])]
    outer = np.array(usable[0], dtype=np.float32) * 0.55
    inner = np.array(usable[(index % max(1, len(usable) - 1)) + 1] if len(usable) > 1 else usable[0], dtype=np.float32)
    yy, xx = np.mgrid[0:size, 0:size] / (size - 1)
    dist = np.sqrt((xx - light[0]) ** 2 + (yy - light[1]) ** 2)
    t = np.clip(dist / 0.75, 0, 1)[..., None] ** 0.8
    img = inner * (1 - t) + outer * t
    if rim:
        rim_dist = np.sqrt((xx - rim[0]) ** 2 + (yy - rim[1]) ** 2)
        img = img + (inner * 0.35) * np.clip(1 - rim_dist / 0.45, 0, 1)[..., None]
    return Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), 'RGB')


def centre_of(mask):
    """Centre of mass of a mask as fractions of its size, or None if empty."""
    import numpy as np
    a = np.asarray(mask, dtype=np.float32)
    total = a.sum()
    if total <= 0:
        return None
    ys, xs = np.mgrid[0:a.shape[0], 0:a.shape[1]]
    return float((xs * a).sum() / total / a.shape[1]), float((ys * a).sum() / total / a.shape[0])


def placement_words(point):
    """'left', 'top right', 'centre'... for the prompt."""
    if point is None:
        return 'centre'
    x, y = point
    horiz = 'left' if x < 0.42 else 'right' if x > 0.58 else ''
    vert = 'top' if y < 0.42 else 'bottom' if y > 0.58 else ''
    return f'{vert} {horiz}'.strip() or 'centre'




# --- Colour consistency (Reinhard transfer in CIE Lab) ------------------------
def _rgb_to_lab(rgb):
    import numpy as np
    c = rgb / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 0.008856, np.cbrt(xyz), 7.787 * xyz + 16 / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def _lab_to_rgb(lab):
    import numpy as np
    fy = (lab[..., 0] + 16) / 116
    f = np.stack([fy + lab[..., 1] / 500, fy, fy - lab[..., 2] / 200], axis=-1)
    xyz = np.where(f > 0.2069, f ** 3, (f - 16 / 116) / 7.787) * np.array([0.95047, 1.0, 1.08883])
    m = np.array([[3.2406, -1.5372, -0.4986], [-0.9689, 1.8758, 0.0415], [0.0557, -0.2040, 1.0570]])
    c = np.clip(xyz @ m.T, 0, 1)
    c = np.where(c > 0.0031308, 1.055 * c ** (1 / 2.4) - 0.055, 12.92 * c)
    return np.clip(c * 255, 0, 255)


def parse_theme_colors(values):
    """RGB tuples from the theme's own colours (theme.xml / the creator's
    Colors tab, 'AARRGGBB'), skipping nearly transparent ones and duplicates."""
    out = []
    for v in values or []:
        h = str(v).strip().lstrip('#')
        if len(h) == 6:
            h = 'ff' + h
        if len(h) != 8:
            continue
        try:
            a, rgb = int(h[:2], 16), tuple(int(h[i:i + 2], 16) for i in (2, 4, 6))
        except ValueError:
            continue
        if a >= 0x40 and rgb not in out:
            out.append(rgb)
    return out


def chromatic(colors):
    """Colours with real hue (not white, black or grey)."""
    return [c for c in colors if _hsv(c)[1] >= 0.25 and _hsv(c)[2] >= 0.25]


def palette_stats(paths):
    """Mean/std of the whole wallpaper set in Lab: the theme's shared palette."""
    import numpy as np
    labs = [_rgb_to_lab(np.asarray(load_rgb(p, (160, 90)), dtype=np.float64)).reshape(-1, 3) for p in paths]
    allpix = np.concatenate(labs)
    return allpix.mean(axis=0), allpix.std(axis=0) + 1e-6


def match_palette(img, stats, amount):
    """Pull the icon toward the theme's colours (hue and overall tone) without
    ever lowering its contrast or saturation: copying a muted theme's spread
    (e.g. one with black-and-white wallpapers) drained the glow and ornament
    out of every icon, so the art direction only survived on vivid themes."""
    import numpy as np
    from PIL import Image
    if amount <= 0:
        return img
    rgb = np.asarray(img, dtype=np.float64)
    lab = _rgb_to_lab(rgb)
    mean, std = lab.reshape(-1, 3).mean(axis=0), lab.reshape(-1, 3).std(axis=0) + 1e-6
    t_mean, t_std = stats
    t_std = np.maximum(t_std, std)  # never flatten contrast (L) or saturation (a, b)
    moved = (lab - mean) / std * t_std + t_mean
    out = _lab_to_rgb(lab + (moved - lab) * amount)
    return Image.fromarray(out.astype(np.uint8), 'RGB')


# --- Character cut-outs ----------------------------------------------------------
# --- Text removal -----------------------------------------------------------------
def load_text_detector():
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    return ort.InferenceSession(hf_hub_download(TEXT_DET_REPO, TEXT_DET_FILE), providers=['CPUExecutionProvider'])


def rank_filter(img, size, use_max=True):
    """Same pixels as img.filter(ImageFilter.MaxFilter(size)) (or MinFilter)
    for 'L' images, but fast for large sizes: a square max/min is a row
    max/min followed by a column one, done with numpy. Pillow's rank filter
    sorts every window (very slow at 768px with large windows). Edges are
    replicated, as Pillow does."""
    import numpy as np
    from PIL import Image, ImageFilter
    if size <= 5 or img.mode != 'L':
        return img.filter((ImageFilter.MaxFilter if use_max else ImageFilter.MinFilter)(size))
    op = np.maximum if use_max else np.minimum
    r = size // 2
    a = np.asarray(img)
    for axis in (0, 1):
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r, r)
        p = np.pad(a, pad, mode='edge')
        n = a.shape[axis]
        out = p.take(range(0, n), axis=axis)
        for k in range(1, 2 * r + 1):
            out = op(out, p.take(range(k, k + n), axis=axis))
        a = out
    return Image.fromarray(np.ascontiguousarray(a), 'L')


def item_silhouette(item, mask):
    """A simple silhouette of an iconic item placed on a symbol ('L', same size
    as mask), or None for items without one. Drawn into the symbol pass's
    starting image and outline guide: from wording alone the model never drew
    the item, but it styles a rough shape it is given (like the symbol itself)."""
    import math
    from PIL import Image, ImageDraw
    box = mask.point(lambda v: 255 if v > 127 else 0).getbbox()
    if not box:
        return None
    size = mask.width
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    cx = (x0 + x1) / 2
    clamp = lambda v: max(2, min(size - 3, v))
    top = clamp(y0 - 0.32 * h)          # how far above the symbol an item may reach
    out = Image.new('L', (size, size), 0)
    d = ImageDraw.Draw(out)

    def stroke(points, r0, r1):
        """A tapering stroke along a quadratic curve (horns, scythe blade)."""
        (ax, ay), (bx, by), (qx, qy) = points
        for i in range(41):
            t = i / 40
            x = (1 - t) ** 2 * ax + 2 * (1 - t) * t * bx + t ** 2 * qx
            y = (1 - t) ** 2 * ay + 2 * (1 - t) * t * by + t ** 2 * qy
            r = r0 + (r1 - r0) * t
            d.ellipse((x - r, y - r, x + r, y + r), fill=255)

    if item == 'horns':
        for sx in (-1, 1):
            base = (cx + sx * 0.28 * w, y0 + 0.12 * h)
            stroke([base, (cx + sx * 0.5 * w, y0 - 0.05 * h), (clamp(cx + sx * 0.58 * w), top)], 0.07 * w, 0.012 * w)
    elif item in ('crown', 'tiara'):
        low, high = y0 + 0.04 * h, top if item == 'crown' else clamp(y0 - 0.18 * h)
        xs = [x0 + 0.18 * w + i * 0.16 * w for i in range(5)]
        pts = [(xs[0], low)] + [(x, high if i % 2 == 0 else (low + high) / 2) for i, x in enumerate(xs)] + [(xs[-1], low)]
        d.polygon(pts, fill=255)
    elif item == 'halo':
        ry = 0.07 * h
        d.ellipse((cx - 0.32 * w, top, cx + 0.32 * w, top + 2 * ry), outline=255, width=max(3, int(0.05 * w)))
    elif item in ('animal ears', 'cat ears', 'fox ears'):
        for sx in (-1, 1):
            bx = cx + sx * 0.3 * w
            d.polygon([(bx - 0.14 * w, y0 + 0.12 * h), (bx + 0.14 * w, y0 + 0.12 * h), (clamp(bx + sx * 0.08 * w), top)], fill=255)
    elif item == 'bunny ears':
        for sx in (-1, 1):
            bx = cx + sx * 0.18 * w
            d.ellipse((bx - 0.08 * w, top, bx + 0.08 * w, y0 + 0.2 * h), fill=255)
    elif item == 'hat':
        d.ellipse((x0 + 0.05 * w, y0 - 0.06 * h, x1 - 0.05 * w, y0 + 0.08 * h), fill=255)
        d.rounded_rectangle((cx - 0.24 * w, top, cx + 0.24 * w, y0 + 0.02 * h), radius=int(0.06 * w), fill=255)
    elif item == 'headphones':
        d.arc((x0 - 0.04 * w, top, x1 + 0.04 * w, y0 + 0.9 * h), 180, 360, fill=255, width=max(4, int(0.07 * w)))
        for sx in (x0 - 0.1 * w, x1 - 0.02 * w):
            d.rounded_rectangle((clamp(sx), y0 + 0.3 * h, clamp(sx + 0.12 * w), y0 + 0.62 * h), radius=int(0.04 * w), fill=255)
    elif item in ('ribbon', 'hair ornament', 'hairclip', 'hair flower'):
        bx, by = clamp(x1 - 0.12 * w), clamp(y0 - 0.06 * h)
        if item == 'hair flower':
            for k in range(5):
                a = k * 2 * math.pi / 5
                px, py = bx + math.cos(a) * 0.09 * w, by + math.sin(a) * 0.09 * w
                d.ellipse((px - 0.07 * w, py - 0.07 * w, px + 0.07 * w, py + 0.07 * w), fill=255)
        else:
            d.polygon([(bx, by), (bx - 0.2 * w, by - 0.12 * w), (bx - 0.2 * w, by + 0.12 * w)], fill=255)
            d.polygon([(bx, by), (bx + 0.2 * w, by - 0.12 * w), (bx + 0.2 * w, by + 0.12 * w)], fill=255)
            d.ellipse((bx - 0.05 * w, by - 0.05 * w, bx + 0.05 * w, by + 0.05 * w), fill=255)
    elif item in ('sword', 'katana', 'staff', 'wand', 'scythe'):
        ax, ay = clamp(x0 - 0.12 * w), clamp(y1 + 0.1 * h)
        bx, by = clamp(x1 + 0.12 * w), top
        d.line((ax, ay, bx, by), fill=255, width=max(4, int((0.07 if item in ('sword', 'katana') else 0.045) * w)))
        if item in ('sword', 'katana'):
            gx, gy = ax + (bx - ax) * 0.22, ay + (by - ay) * 0.22
            d.line((gx - 0.12 * w, gy - 0.12 * w, gx + 0.12 * w, gy + 0.12 * w), fill=255, width=max(4, int(0.05 * w)))
        elif item == 'scythe':
            stroke([(bx, by), (bx - 0.3 * w, by - 0.02 * h), (bx - 0.45 * w, by + 0.22 * h)], 0.05 * w, 0.01 * w)
        else:
            d.ellipse((bx - 0.08 * w, by - 0.08 * w, bx + 0.08 * w, by + 0.08 * w), fill=255)
    elif item == 'microphone':
        mx, my = clamp(x1 + 0.02 * w), clamp(y0 + 0.05 * h)
        d.line((mx, my, clamp(mx + 0.18 * w), clamp(my + 0.5 * h)), fill=255, width=max(4, int(0.05 * w)))
        d.ellipse((mx - 0.1 * w, my - 0.1 * w, mx + 0.1 * w, my + 0.1 * w), fill=255)
    elif item == 'wings':
        for sx in (-1, 1):
            ex = clamp(cx + sx * 0.62 * w)
            d.polygon([(cx + sx * 0.4 * w, y0 + 0.3 * h), (ex, y0 - 0.1 * h), (ex, y0 + 0.3 * h), (cx + sx * 0.45 * w, y0 + 0.6 * h)], fill=255)
    else:
        return None
    return out


def symbol_gaps(mask):
    """Where a symbol's design has gaps the background must show through:
    holes inside it (eyes, cut-outs) and the narrow spaces between its
    separate pieces (between the content manager's pictures, the www dots).
    A drawn symbol filled these with colour. Returns an 'L' mask, slightly
    shrunk so the drawn edge along each gap is kept."""
    import numpy as np
    from PIL import Image
    on = np.asarray(mask) > 127
    k = 2 * (mask.width // 24) + 1
    closed = np.asarray(rank_filter(rank_filter(mask, k), k, use_max=False)) > 127
    filled = (on * 255).astype(np.uint8)
    holes = ~on & ~border_region(filled)
    gaps = Image.fromarray((((closed & ~on) | holes) * 255).astype(np.uint8), 'L')
    return rank_filter(gaps, 2 * max(2, mask.width // 192) + 1, use_max=False)


def symbol_parts(mask, min_share=0.02):
    """The separate pieces of a symbol shape (4-connected), as boolean masks,
    ignoring specks under min_share of the whole shape."""
    import numpy as np
    on = np.asarray(mask) > 127
    h, w = on.shape
    parent, runs, prev = [], [], []

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for y in range(h):
        row = np.concatenate(([False], on[y], [False]))
        d = np.diff(row.astype(np.int8))
        cur = []
        for s0, e0 in zip(np.flatnonzero(d == 1).tolist(), (np.flatnonzero(d == -1) - 1).tolist()):
            idx = len(runs)
            runs.append((y, s0, e0))
            parent.append(idx)
            for pidx, ps, pe in prev:
                if ps <= e0 and pe >= s0:
                    a, b = find(idx), find(pidx)
                    if a != b:
                        parent[a] = b
            cur.append((idx, s0, e0))
        prev = cur
    groups = {}
    for i, (y, s0, e0) in enumerate(runs):
        groups.setdefault(find(i), []).append((y, s0, e0))
    total = on.sum()
    parts = []
    for group in groups.values():
        part = np.zeros((h, w), dtype=bool)
        for y, s0, e0 in group:
            part[y, s0:e0 + 1] = True
        if total and part.sum() >= min_share * total:
            parts.append(part)
    return parts


def border_region(arr):
    """Boolean mask of the 4-connected region of pixels equal to arr[0, 0]
    that contains (0, 0): what ImageDraw.floodfill(img, (0, 0), ...) fills,
    computed from row runs (fast) instead of pixel by pixel in Python."""
    import numpy as np
    target = arr == arr[0, 0]
    h, w = target.shape
    parent = []

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    runs, prev = [], []
    for y in range(h):
        row = np.concatenate(([False], target[y], [False]))
        d = np.diff(row.astype(np.int8))
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1) - 1
        cur = []
        for s, e in zip(starts.tolist(), ends.tolist()):
            idx = len(runs)
            runs.append((y, s, e))
            parent.append(idx)
            for pidx, ps, pe in prev:
                if ps <= e and pe >= s:
                    a, b = find(idx), find(pidx)
                    if a != b:
                        parent[a] = b
            cur.append((idx, s, e))
        prev = cur
    out = np.zeros((h, w), dtype=bool)
    if not runs or runs[0][0] != 0 or runs[0][1] != 0:
        return out
    root = find(0)
    for i, (y, s, e) in enumerate(runs):
        if find(i) == root:
            out[y, s:e + 1] = True
    return out


def text_mask(session, img, keep=None):
    """Mask (L, img size) of lettering in an image, grown a little so whole
    words are covered. Pixels inside `keep` (the symbol) are never flagged:
    symbol strokes can look like letters to the detector."""
    import numpy as np
    from PIL import Image, ImageChops, ImageFilter
    size = (img.width // 32 * 32 or 32, img.height // 32 * 32 or 32)
    rgb = np.asarray(img.convert('RGB').resize(size, Image.Resampling.BICUBIC), dtype=np.float32) / 255
    # PaddleOCR models are trained on BGR input with ImageNet normalisation.
    x = (rgb[..., ::-1] - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    prob = session.run(None, {session.get_inputs()[0].name: x.transpose(2, 0, 1)[None].astype(np.float32)})[0][0, 0]
    mask = Image.fromarray(((prob > TEXT_THRESHOLD) * 255).astype(np.uint8), 'L').resize(img.size, Image.Resampling.NEAREST)
    mask = rank_filter(mask, 9)
    if keep is not None:
        mask = ImageChops.subtract(mask, rank_filter(keep, 15))
    return mask


def has_text(mask, area=None):
    import numpy as np
    return float((np.asarray(mask) > 127).mean()) > (0.0005 if area is None else area)


def strip_text(session, cut):
    """Erase wallpaper lettering (logos, titles) from a character cut-out."""
    from PIL import Image, ImageChops
    flat = Image.new('RGB', cut.size, (128, 128, 128))
    flat.paste(cut, mask=cut.getchannel('A'))
    found = text_mask(session, flat)
    if has_text(found):
        cut = cut.copy()
        cut.putalpha(ImageChops.subtract(cut.getchannel('A'), found))
    return cut


def load_segmenter():
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    return ort.InferenceSession(hf_hub_download(SEGMENT_REPO, SEGMENT_FILE), providers=['CPUExecutionProvider'])


def segment(session, img):
    """Soft character mask (0..1) for a wallpaper, same size as the image."""
    import numpy as np
    from PIL import Image
    w0, h0 = img.size
    s = SEGMENT_SIZE
    h, w = (s, int(s * w0 / h0)) if h0 > w0 else (int(s * h0 / w0), s)
    arr = np.asarray(img.resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32) / 255
    padded = np.zeros((s, s, 3), dtype=np.float32)
    ph, pw = (s - h) // 2, (s - w) // 2
    padded[ph:ph + h, pw:pw + w] = arr
    out = np.squeeze(session.run(None, {session.get_inputs()[0].name: padded.transpose(2, 0, 1)[None]})[0])
    mask = Image.fromarray((np.clip(out[ph:ph + h, pw:pw + w], 0, 1) * 255).astype(np.uint8), 'L')
    return np.asarray(mask.resize((w0, h0), Image.Resampling.BILINEAR), dtype=np.float32) / 255


def _components(binary):
    """4-connected components of a small boolean grid: list of (area, cells)."""
    h, w = binary.shape
    seen = [[False] * w for _ in range(h)]
    comps = []
    for y in range(h):
        for x in range(w):
            if binary[y, x] and not seen[y][x]:
                stack, cells = [(y, x)], []
                seen[y][x] = True
                while stack:
                    cy, cx = stack.pop()
                    cells.append((cy, cx))
                    for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                        if 0 <= ny < h and 0 <= nx < w and binary[ny, nx] and not seen[ny][nx]:
                            seen[ny][nx] = True
                            stack.append((ny, nx))
                comps.append((len(cells), cells))
    return comps


def load_face_detector():
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    return ort.InferenceSession(hf_hub_download(FACE_REPO, FACE_FILE), providers=['CPUExecutionProvider'])


def character_quality(char, face_session):
    """How good a picture of a character is for an icon (higher is better):
    a visible face (not a back-of-head shot), a bigger and sharper head,
    and fewer cuts and seams."""
    import numpy as np
    from PIL import Image, ImageFilter
    crop = char.get('head_crop')
    if crop is None:
        return 0.0
    face = 1.0 if detect_heads(face_session, crop, size=256, threshold=FACE_THRESHOLD) else 0.0
    size = min(1.0, crop.width / 160)
    g = np.asarray(crop.convert('L').resize((96, 96)).filter(ImageFilter.FIND_EDGES), dtype=np.float32)
    sharp = min(1.0, float(g.var()) / 1500)
    cuts = sum(1 for v in char['sides'].values() if v) + min(3, len(char.get('seams', [])) // 10)
    return 1.5 * face + size + 0.5 * sharp - 0.3 * cuts


def load_head_detector():
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    return ort.InferenceSession(hf_hub_download(HEAD_REPO, HEAD_FILE), providers=['CPUExecutionProvider'])


def detect_heads(session, img, size=640, threshold=HEAD_THRESHOLD):
    """Head boxes (x0, y0, x1, y1) in image pixels, after non-maximum suppression."""
    import numpy as np
    from PIL import Image
    w0, h0 = img.size
    scale = size / max(w0, h0)
    w, h = max(32, int(round(w0 * scale / 32) * 32)), max(32, int(round(h0 * scale / 32) * 32))
    x = np.asarray(img.convert('RGB').resize((w, h), Image.Resampling.BILINEAR), dtype=np.float32) / 255
    out = session.run(None, {session.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0][0]
    if out.shape[0] > out.shape[1]:
        out = out.T
    cx, cy, bw, bh, score = out[:5]
    keep = score > threshold
    boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1)[keep]
    picked = []
    for i in score[keep].argsort()[::-1]:
        b = boxes[i]
        if all(_iou(b, boxes[j]) < 0.5 for j in picked):
            picked.append(i)
    return [tuple(float(v) for v in boxes[i] * [w0 / w, h0 / h, w0 / w, h0 / h]) for i in picked]


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    return inter / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter + 1e-6)


def _cut_from_region(img, alpha, region, grid_w, grid_h, head=None):
    """RGBA cut-out of one grid region (soft edges from the full-res mask),
    the wallpaper sides it touches, and the head box relative to the crop."""
    import numpy as np
    from PIL import Image, ImageFilter
    ys, xs = np.where(region)
    # The per-person region comes from a coarse grid: enlarge it smoothly and
    # soften its border, or edges between neighbours come out stair-stepped.
    gate = Image.fromarray((region * 255).astype(np.uint8), 'L').resize(img.size, Image.Resampling.BILINEAR)
    cell = max(1, round(img.width / grid_w))
    gate = rank_filter(gate, 2 * cell + 1).filter(ImageFilter.GaussianBlur(cell))
    soft = (alpha * (np.asarray(gate, dtype=np.float32) / 255) * 255).astype(np.uint8)
    fx, fy = img.width / grid_w, img.height / grid_h
    x0, x1 = int(xs.min() * fx), int((xs.max() + 1) * fx)
    y0, y1 = int(ys.min() * fy), int((ys.max() + 1) * fy)
    if head:
        x0, y0 = min(x0, int(head[0])), min(y0, int(head[1]))
        x1, y1 = max(x1, int(head[2])), max(y1, int(head[3]))
    cut = img.convert('RGBA')
    cut.putalpha(Image.fromarray(soft, 'L'))
    cut = cut.crop((x0, y0, x1, y1))
    sides = {'left': xs.min() == 0, 'right': xs.max() == grid_w - 1, 'top': ys.min() == 0, 'bottom': ys.max() == grid_h - 1}
    rel_head = (head[0] - x0, head[1] - y0, head[2] - x0, head[3] - y0) if head else None
    head_crop = None
    if head:
        pad = 0.08 * (head[2] - head[0])
        head_crop = img.crop((max(0, int(head[0] - pad)), max(0, int(head[1] - pad)),
                              min(img.width, int(head[2] + pad)), min(img.height, int(head[3] + pad))))
    return {'image': cut, 'sides': sides, 'head': rel_head, 'head_crop': head_crop,
            'origin': (x0, y0), 'cell': (fx, fy)}


def own_alpha(path):
    """The image's own transparency (0..1) for renders with a cut-out
    background, or None when the image is opaque."""
    import numpy as np
    from PIL import Image
    with Image.open(path) as img:
        if 'A' not in img.getbands() and 'transparency' not in img.info:
            return None
        a = np.asarray(img.convert('RGBA').getchannel('A'), dtype=np.float32) / 255
    return a if a.min() < 0.9 else None


def seam_points(piece, fg, char):
    """Cut-out pixel positions (grid-cell centres) where the figure continues
    past this piece: cuts made by the reach limit or a neighbour's area."""
    import numpy as np
    grown = piece.copy()
    grown[1:, :] |= piece[:-1, :]; grown[:-1, :] |= piece[1:, :]
    grown[:, 1:] |= piece[:, :-1]; grown[:, :-1] |= piece[:, 1:]
    contact = grown & fg & ~piece
    (fx, fy), (ox, oy) = char['cell'], char['origin']
    return [((x + 0.5) * fx - ox, (y + 0.5) * fy - oy) for y, x in zip(*np.where(contact))]


def find_characters(session, head_session, paths, masks=None, limit=MAX_CHARACTERS):
    """One cut-out per character in the wallpapers.

    With detected heads, the segmentation mask is split between the heads
    (each body pixel goes to its nearest head, bodies hang below their head),
    and only the connected shape holding the head is kept, so groups become
    individuals and stray fragments (loose hair, props) are dropped.
    Characters whose head is cut by two or more wallpaper edges are skipped.
    Wallpapers with no detectable heads (some cartoon styles) fall back to
    large, solid shapes only.

    Returns [{'image', 'sides', 'head'}], most complete and largest first."""
    import numpy as np
    from PIL import Image
    found = []
    for path in paths:
        img = load_rgb(path)
        # Transparent renders already are cut-outs: their own alpha beats segmentation.
        alpha = own_alpha(path)
        if alpha is None:
            alpha = segment(session, img)
        if masks is not None:
            masks[path] = alpha  # reused for the character-free style references
        grid_w = 240
        grid_h = max(1, round(grid_w * img.height / img.width))
        fg = np.asarray(Image.fromarray((alpha * 255).astype(np.uint8), 'L').resize((grid_w, grid_h)), dtype=np.uint8) > 127
        # Every detected head claims its own area (even ones too small to use),
        # or its hair and body leak into a neighbour's cut-out.
        all_heads = detect_heads(head_session, img)
        heads = all_heads
        if any(b[2] - b[0] >= MIN_HEAD_PX for b in heads):
            sx, sy = grid_w / img.width, grid_h / img.height
            yy, xx = np.mgrid[0:grid_h, 0:grid_w]
            costs = []
            for x0, y0, x1, y1 in heads:
                hx, hy, hs = (x0 + x1) / 2 * sx, (y0 + y1) / 2 * sy, max(1.0, (x1 - x0) * sx)
                dy = (yy - hy) / hs
                costs.append(((xx - hx) / hs) ** 2 + (dy * np.where(dy > 0, 0.35, 1.0)) ** 2)
            costs = np.stack(costs)
            owner, best = costs.argmin(axis=0), costs.min(axis=0)
            for k, head in enumerate(heads):
                if head[2] - head[0] < MIN_HEAD_PX:
                    continue  # claims its area above, but too small to use
                # A head cut by one wallpaper edge is fine (that side is anchored
                # to the icon's edge later); cut on two or more sides is not.
                if (head[0] <= 2) + (head[1] <= 2) + (head[2] >= img.width - 2) + (head[3] >= img.height - 2) >= 2:
                    continue
                region = fg & (owner == k) & (best < HEAD_REACH ** 2)
                hx, hy = int((head[0] + head[2]) / 2 * sx), int((head[1] + head[3]) / 2 * sy)
                comps = _components(region)
                if not comps:
                    continue
                # The shape holding (or nearest to) the head; everything else is a stray piece.
                cells = min(comps, key=lambda c: min((cy - hy) ** 2 + (cx - hx) ** 2 for cy, cx in c[1]))[1]
                piece = np.zeros_like(region)
                ys, xs = zip(*cells)
                piece[list(ys), list(xs)] = True
                char = _cut_from_region(img, alpha, piece, grid_w, grid_h, head)
                char['source'] = path
                # Seams: where the figure continues past this cut-out (the reach
                # limit, or a neighbour's area). They can be diagonal lines through
                # the picture, so framings that show them are rejected.
                char['seams'] = seam_points(piece, fg, char)
                completeness = -sum(char['sides'].values()) - min(3, len(char['seams']) // 10)
                found.append(((completeness, head[2] - head[0]), char))
        else:
            for area, cells in _components(fg):
                if area < 0.06 * grid_w * grid_h:
                    continue
                ys, xs = zip(*cells)
                box_w, box_h = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
                edges = (min(xs) == 0) + (max(xs) == grid_w - 1) + (min(ys) == 0) + (max(ys) == grid_h - 1)
                # Background chunks and logos, not characters.
                if edges >= 2 or area / (box_w * box_h) > 0.85 or box_w > 2.2 * box_h:
                    continue
                piece = np.zeros_like(fg)
                piece[list(ys), list(xs)] = True
                char = _cut_from_region(img, alpha, piece, grid_w, grid_h)
                if np.asarray(char['image'].getchannel('A'), dtype=np.float32).mean() < 60:
                    continue
                found.append(((-sum(char['sides'].values()), area), char))
    found.sort(key=lambda item: item[0], reverse=True)
    return [char for _score, char in found[:limit]]


# Framings of the real art (never redrawn, so never malformed), measured from
# the detected head: (left, top, right, bottom) margins in head widths/heights.
HEAD_FRAMINGS = {'face': (0.45, 0.25, 0.45, 0.6), 'bust': (1.0, 0.25, 1.0, 1.9), 'full': None}
# Without a head box: height as a multiple of the figure's width.
FRAMING_HEIGHT = {'face': 0.75, 'bust': 1.25, 'full': None}
LAYOUT_FRAMINGS = {
    'peek': ['face', 'bust'], 'lean': ['bust', 'full'], 'sit': ['full', 'bust'],
    'bust': ['bust', 'face'], 'portrait': ['face', 'bust'], 'example': ['face', 'bust', 'full'],
}
MAX_TILT = 12      # degrees, for figures with no cut sides
CUT_TILT = 4       # cut figures sit against an icon edge; a big tilt would expose the cut


def scenery_reference(path, alpha, size):
    """A style reference with as few characters as possible, fully sharp.

    The square crop of the wallpaper with the fewest characters is used.
    Blurring the remaining figures taught the style adapter blurry patches,
    which came back as smudged background objects; invented people are
    caught by the face check instead."""
    import numpy as np
    from PIL import Image
    with Image.open(path) as src:
        img = src.convert('RGB')
    w, h = img.size
    side = min(w, h)
    box = ((w - side) // 2, (h - side) // 2)
    if alpha is not None:
        a = np.asarray(alpha, dtype=np.float32)
        steps = 8
        best = None
        for k in range(steps + 1):
            x = (w - side) * k // steps
            y = (h - side) * k // steps
            coverage = a[y:y + side, x:x + side].mean()
            if best is None or coverage < best[0]:
                best = (coverage, (x, y))
        box = best[1]
    return img.crop((box[0], box[1], box[0] + side, box[1] + side)).resize((size, size), Image.Resampling.LANCZOS)


def scenery_detail(img):
    """Share of edge pixels (at 256px): how much actual content a picture has."""
    import numpy as np
    from PIL import ImageFilter
    small = img.convert('L').resize((256, 256))
    return float((np.asarray(small.filter(ImageFilter.FIND_EDGES)) > 24).mean())


def environment_crops(path, alpha, size, rng, tries=12):
    """Seeded pieces of a picture's environment (random zoom and position),
    fewest characters first, so every generation sets its scenes somewhere
    else in the theme's world: [(character share, crop)]. Empty for cut-out
    renders (transparent artwork has no environment)."""
    import numpy as np
    from PIL import Image
    with Image.open(path) as src:
        if 'A' in src.getbands() or src.mode == 'P':
            a = np.asarray(src.convert('RGBA').getchannel('A'))
            if (a < 250).mean() > 0.1:
                return []
        img = src.convert('RGB')
    w, h = img.size
    a = np.asarray(alpha, dtype=np.float32) if alpha is not None else None
    if a is not None and a.shape != (h, w):
        a = None
    boxes = []
    for _ in range(tries):
        side = max(16, int(min(w, h) * rng.uniform(*SCENE_ZOOM)))
        x, y = rng.randint(0, w - side), rng.randint(0, h - side)
        coverage = float(a[y:y + side, x:x + side].mean()) if a is not None else 0.0
        boxes.append((coverage, (x, y, x + side, y + side)))
    boxes.sort(key=lambda b: b[0])
    return [(c, img.crop(b).resize((size, size), Image.Resampling.LANCZOS)) for c, b in boxes]


def visible_cuts(img, min_run=6, min_share=0.15):
    """Sides where the visible figure runs into the image edge along a long
    continuous stretch (a slice through the body). A natural silhouette, such
    as the top of the hair or a fingertip, only grazes the edge."""
    import numpy as np
    a = np.asarray(img.getchannel('A')) > 128
    if not a.any():
        return {'left': False, 'right': False, 'top': False, 'bottom': False}

    def longest_run(edge):
        best = run = 0
        for v in edge:
            run = run + 1 if v else 0
            best = max(best, run)
        return best

    h, w = a.shape
    strips = {'left': a[:, :2].any(axis=1), 'right': a[:, -2:].any(axis=1),
              'top': a[:2, :].any(axis=0), 'bottom': a[-2:, :].any(axis=0)}
    return {side: longest_run(edge) >= max(min_run, min_share * len(edge)) for side, edge in strips.items()}


def frame_character(char, framing, rng):
    """One framing of a character. Returns (RGBA, cut sides): a side is 'cut'
    where the framing or the wallpaper border slices through the figure."""
    import numpy as np
    from PIL import Image
    img, sides, head = char['image'], dict(char['sides']), char.get('head')
    box = None
    full_size = img.size
    if head and HEAD_FRAMINGS.get(framing):
        ml, mt, mr, mb = HEAD_FRAMINGS[framing]
        hw, hh = head[2] - head[0], head[3] - head[1]
        box = (max(0, int(head[0] - ml * hw)), max(0, int(head[1] - mt * hh)),
               min(img.width, int(head[2] + mr * hw)), min(img.height, int(head[3] + mb * hh)))
    elif not head and FRAMING_HEIGHT.get(framing) and img.height > img.width * FRAMING_HEIGHT[framing]:
        box = (0, 0, img.width, int(img.width * FRAMING_HEIGHT[framing]))
    if box:
        img = img.crop(box)
    x0, y0, x1, y1 = box or (0, 0) + full_size
    # Seam points inside the framing (away from its bottom edge, where a bust
    # crop is cut anyway) are visible diagonal cuts.
    margin = 0.06 * (y1 - y0)
    seams = sum(1 for sx, sy in char.get('seams', []) if x0 <= sx < x1 and y0 <= sy < y1 - margin)
    # What is visibly cut, judged on the final picture itself: covers the
    # framing crop, the wallpaper border and cuts inside the figure (reach
    # limit, a neighbour's area) alike.
    sides = visible_cuts(img)
    sides['seam'] = seams >= SEAM_TOLERANCE
    img = fade_cut_sides(img, sides, fraction=0.12)
    # Side-cut figures sit flush against an icon edge: any tilt would show the cut.
    tilt = 0 if (sides['left'] or sides['right']) else CUT_TILT if any(sides.values()) else MAX_TILT
    img = img.rotate(rng.uniform(-tilt, tilt), resample=Image.Resampling.BICUBIC, expand=True)
    bbox = img.getchannel('A').getbbox()
    return (img.crop(bbox) if bbox else img), sides


def same_character_groups(chars):
    """{index: [indices of the same character]} from the identity model, using
    only confident matches that agree both ways and never two people from the
    same wallpaper (they are different characters by definition)."""
    import numpy as np
    from huggingface_hub import hf_hub_download
    import onnxruntime as ort
    from PIL import Image
    idx = [i for i, c in enumerate(chars) if c.get('head_crop') is not None]
    if len(idx) < 2:
        return {}
    feat = ort.InferenceSession(hf_hub_download(IDENTITY_REPO, f'{IDENTITY_DIR}/model_feat.onnx'), providers=['CPUExecutionProvider'])
    metric = ort.InferenceSession(hf_hub_download(IDENTITY_REPO, f'{IDENTITY_DIR}/model_metrics.onnx'), providers=['CPUExecutionProvider'])
    x = np.stack([(np.asarray(chars[i]['head_crop'].resize((384, 384), Image.Resampling.BILINEAR), dtype=np.float32) / 255 - 0.5) / 0.5
                  for i in idx]).transpose(0, 3, 1, 2).astype(np.float32)
    diff = metric.run(None, {metric.get_inputs()[0].name: feat.run(None, {feat.get_inputs()[0].name: x})[0]})[0]
    n = len(idx)
    for a in range(n):
        for b in range(n):
            if a == b or chars[idx[a]].get('source') == chars[idx[b]].get('source'):
                diff[a, b] = 9.0
    def confident(a):
        order = np.argsort(diff[a])
        best = order[0]
        ok = diff[a, best] < SAME_CHARACTER_DIFF and (n < 3 or diff[a, order[1]] - diff[a, best] > SAME_CHARACTER_MARGIN)
        return best if ok else None
    groups = {}
    for a in range(n):
        b = confident(a)
        if b is not None and confident(b) == a:
            groups.setdefault(idx[a], []).append(idx[b])
    return groups


# Pose library for redrawn characters: upper-body OpenPose skeletons (18 COCO
# keypoints as fractions of a square canvas, None = off-canvas). Image left is
# the character's right.
_POSE_BASE = {0: (0.50, 0.22), 1: (0.50, 0.36), 2: (0.38, 0.38), 5: (0.62, 0.38), 8: (0.43, 0.78), 11: (0.57, 0.78),
              14: (0.47, 0.19), 15: (0.53, 0.19), 16: (0.44, 0.21), 17: (0.56, 0.21)}
# name -> (explicit description for the prompt, arm keypoints). Poses with the
# hands right beside the face are left out: that is where small models grow
# limbs out of heads.
POSES = {
    'waving': ('waving, right arm raised to the side above the shoulder, arm attached at the shoulder, left arm relaxed at the side',
               {3: (0.33, 0.55), 4: (0.35, 0.72), 6: (0.76, 0.30), 7: (0.84, 0.14)}),
    'arms crossed': ('arms crossed over the chest, both forearms visible, hands tucked at the elbows',
                     {3: (0.36, 0.55), 4: (0.60, 0.50), 6: (0.64, 0.55), 7: (0.40, 0.50)}),
    'pointing': ('pointing to the side with one straight arm, other arm relaxed at the side',
                 {3: (0.33, 0.55), 4: (0.35, 0.72), 6: (0.72, 0.40), 7: (0.90, 0.40)}),
    'cheering': ('cheering with both arms raised high to the sides, arms attached at the shoulders, hands open',
                 {3: (0.28, 0.28), 4: (0.22, 0.10), 6: (0.72, 0.28), 7: (0.78, 0.10)}),
    'hands on hips': ('standing confidently with both hands on the hips, elbows out to the sides',
                      {3: (0.26, 0.55), 4: (0.40, 0.74), 6: (0.74, 0.55), 7: (0.60, 0.74)}),
    'reaching out': ('reaching one open hand forward toward the viewer, other arm relaxed at the side',
                     {3: (0.33, 0.55), 4: (0.35, 0.72), 6: (0.66, 0.50), 7: (0.74, 0.62)}),
}
EXPRESSIONS = ['smiling', 'laughing', 'surprised', 'determined', 'winking', 'shy blushing', 'confident']
_POSE_LIMBS = [(1, 2), (1, 5), (2, 3), (3, 4), (5, 6), (6, 7), (1, 8), (1, 11), (1, 0), (0, 14), (14, 16), (0, 15), (15, 17)]
_POSE_COLORS = [(255, 0, 0), (255, 85, 0), (255, 170, 0), (255, 255, 0), (170, 255, 0), (85, 255, 0), (0, 255, 0),
                (0, 255, 85), (0, 255, 170), (0, 255, 255), (0, 170, 255), (0, 85, 255), (0, 0, 255), (85, 0, 255),
                (170, 0, 255), (255, 0, 255), (255, 0, 170), (255, 0, 85)]


def draw_pose(name, size=GEN_SIZE):
    """A pose from the library in the standard OpenPose skeleton colours."""
    from PIL import Image, ImageDraw
    points = {**_POSE_BASE, **POSES[name][1]}
    img = Image.new('RGB', (size, size), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    for i, (a, b) in enumerate(_POSE_LIMBS):
        if a in points and b in points:
            (xa, ya), (xb, yb) = points[a], points[b]
            draw.line((xa * size, ya * size, xb * size, yb * size), fill=tuple(int(c * 0.6) for c in _POSE_COLORS[i]), width=8)
    for k, (x, y) in points.items():
        draw.ellipse((x * size - 5, y * size - 5, x * size + 5, y * size + 5), fill=_POSE_COLORS[k])
    return img


_IDENTITY = []


def identity_diff(crop_a, crop_b):
    """CCIP difference between two head crops (lower = more alike)."""
    import numpy as np
    from huggingface_hub import hf_hub_download
    import onnxruntime as ort
    from PIL import Image
    if not _IDENTITY:
        _IDENTITY.append(ort.InferenceSession(hf_hub_download(IDENTITY_REPO, f'{IDENTITY_DIR}/model_feat.onnx'), providers=['CPUExecutionProvider']))
        _IDENTITY.append(ort.InferenceSession(hf_hub_download(IDENTITY_REPO, f'{IDENTITY_DIR}/model_metrics.onnx'), providers=['CPUExecutionProvider']))
    feat, metric = _IDENTITY
    x = np.stack([(np.asarray(c.convert('RGB').resize((384, 384), Image.Resampling.BILINEAR), dtype=np.float32) / 255 - 0.5) / 0.5
                  for c in (crop_a, crop_b)]).transpose(0, 3, 1, 2).astype(np.float32)
    diff = metric.run(None, {metric.get_inputs()[0].name: feat.run(None, {feat.get_inputs()[0].name: x})[0]})[0]
    return float(diff[0, 1])


def character_reference(char, size=GEN_SIZE):
    """The character on a plain grey square, bottom-centred: the redraw's
    starting image and look reference."""
    from PIL import Image
    img = char['image'].copy()
    img.thumbnail((int(size * 0.9), int(size * 0.9)))
    ref = Image.new('RGB', (size, size), (128, 128, 128))
    ref.paste(img, ((size - img.width) // 2, size - img.height), img)
    return ref


def complete_framing(char, framings, rng, avoid=(), allow_repair=False):
    """A framing of the character with no visible cut, preferring framings not
    in `avoid`. Only a bottom cut is fine (it sits on the icon's bottom edge
    like a portrait). Side and top cuts are never used: pinning a side cut
    flush to the icon edge still showed a half face or body sliced by the
    border. A repair (allow_repair) is only offered for cuts inside the
    figure (seams), which the redraw can paint over; a redraw can't invent
    the missing half of a figure at a border.
    Returns (img, sides, framing) or None."""
    ordered = rng.sample(framings, len(framings))
    ordered.sort(key=lambda f: f in avoid)
    # Last resort: a tight face close-up usually crops above cuts at the
    # shoulders and body (where characters touch neighbours in group shots).
    if 'face' not in ordered:
        ordered.append('face')
    # ...and the whole figure, uncropped: no framing box to slice the hair or shoulders.
    if 'full' not in ordered:
        ordered.append('full')
    for framing in ordered:
        img, sides = frame_character(char, framing, rng)
        if not (sides['top'] or sides['left'] or sides['right'] or sides.get('seam')):
            return img, sides, framing
    if allow_repair and char.get('head_crop') is not None:
        # Cut only inside the figure: the redraw pass can paint over the seam (accuracy-checked).
        for framing in ordered:
            img, sides = frame_character(char, framing, rng)
            if not (sides['top'] or sides['left'] or sides['right']):
                return img, dict(sides, repair=True), framing
    return None


def restore_face(head_session, original, redrawn, figure_alpha):
    """The redraw with the character's original face blended back in."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    a = np.asarray(figure_alpha) > 127
    def on_figure(b):
        patch = a[max(0, int(b[1])):int(b[3]), max(0, int(b[0])):int(b[2])]
        return patch.size > 0 and patch.mean() >= 0.3
    heads = [b for b in detect_heads(head_session, original) if on_figure(b)]
    if not heads:
        return redrawn
    x0, y0, x1, y1 = max(heads, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    rx, ry = (x1 - x0) / 2 * FACE_KEEP_SCALE, (y1 - y0) / 2 * FACE_KEEP_SCALE
    keep = Image.new('L', original.size, 0)
    ImageDraw.Draw(keep).ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=255)
    keep = keep.filter(ImageFilter.GaussianBlur(max(2, rx * 0.15)))
    return Image.composite(original, redrawn, keep)


def figure_match(original, redrawn, figure_alpha):
    """Identity difference between the character as placed and as redrawn,
    over the whole figure (lower = closer)."""
    box = figure_alpha.point(lambda v: 255 if v > 127 else 0).getbbox()
    if not box:
        return None
    return identity_diff(original.crop(box), redrawn.crop(box))


def theme_tags(paths, limit=THEME_MOTIFS):
    """(motifs, traits) of the wallpapers. Motifs: the scenery and objects
    that recur ("classroom", "blood", "sky"); people and clothing tags left
    out. Traits: the theme's iconic features, its recurring characters by name
    ("kiana kaslana") and distinctive accessories ("horns", "microphone")."""
    import csv
    import numpy as np
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    from PIL import Image
    sess = ort.InferenceSession(hf_hub_download(TAGGER_REPO, 'model.onnx'), providers=['CPUExecutionProvider'])
    with open(hf_hub_download(TAGGER_REPO, 'selected_tags.csv'), encoding='utf-8') as f:
        rows = list(csv.DictReader(f))
    size = sess.get_inputs()[0].shape[1]
    count, score, trait_count = {}, {}, {}
    for path in paths:
        try:
            img = Image.open(path).convert('RGBA')
        except OSError:
            continue
        flat = Image.new('RGBA', img.size, (255, 255, 255, 255))
        flat.alpha_composite(img)
        side = max(img.size)
        square = Image.new('RGB', (side, side), (255, 255, 255))
        square.paste(flat.convert('RGB'), ((side - img.width) // 2, (side - img.height) // 2))
        # The tagger takes BGR pixels 0-255, NHWC.
        x = np.asarray(square.resize((size, size), Image.Resampling.BICUBIC), dtype=np.float32)[:, :, ::-1][None]
        probs = sess.run(None, {sess.get_inputs()[0].name: np.ascontiguousarray(x)})[0][0]
        for row, prob in zip(rows, probs):
            tag = row['name'].replace('_', ' ')
            if (row['category'] == '0' and prob > TAGGER_THRESHOLD and tag[:1].isalpha()
                    and not any(word in tag for word in NOT_MOTIFS)):
                count[tag] = count.get(tag, 0) + 1
                score[tag] = score.get(tag, 0.0) + float(prob)
            if row['category'] == '0' and prob > TAGGER_THRESHOLD and tag in ICONIC_DRAWABLE:
                trait_count[tag] = trait_count.get(tag, 0) + 1
    motifs = sorted(count, key=lambda t: (count[t], score[t]), reverse=True)[:limit]
    traits = sorted((t for t, n in trait_count.items() if n >= ICONIC_MIN_COUNT), key=lambda t: -trait_count[t])[:6]
    # Weighted by how strongly each recurs: a theme's signature (Elfen Lied's
    # horns, in 4 wallpapers) is picked more than a passing detail (a ribbon).
    return motifs, {t: trait_count[t] for t in traits}


def art_style_scores(paths):
    """{style label: probability}, averaged over the pictures."""
    import numpy as np
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    from PIL import Image
    sess = ort.InferenceSession(hf_hub_download(ART_STYLE_REPO, ART_STYLE_FILE), providers=['CPUExecutionProvider'])
    scores = []
    for p in paths:
        try:
            im = Image.open(p).convert('RGB').resize((384, 384), Image.Resampling.BILINEAR)
        except OSError:
            continue
        x = ((np.asarray(im, dtype=np.float32) / 255 - 0.5) / 0.5).transpose(2, 0, 1)[None]
        scores.append(sess.run(None, {sess.get_inputs()[0].name: x})[0][0])  # already probabilities
    if not scores:
        return {}
    return dict(zip(ART_STYLE_LABELS, np.mean(scores, axis=0).tolist()))


def detect_art_style(paths):
    """The theme's rendering style: the classifier's scores averaged over the
    wallpapers, so one odd picture doesn't decide it."""
    scores = art_style_scores(paths)
    return max(scores, key=scores.get) if scores else 'illustration'


def load_person_detector():
    import onnxruntime as ort
    from huggingface_hub import hf_hub_download
    return ort.InferenceSession(hf_hub_download(PERSON_REPO, PERSON_FILE), providers=['CPUExecutionProvider'])


def invented_figures(head_session, img, real=None, person_session=None):
    """How many people the model painted itself: heads (and, with a person
    detector, whole bodies, even headless ones) found in the image, ignoring
    the real character cut-out (`real` = its alpha mask)."""
    import numpy as np
    scale = img.width / 512
    found = [b for b in detect_heads(head_session, img) if b[2] - b[0] >= MIN_INVENTED_HEAD_PX * scale]
    if person_session is not None:
        found += [b for b in detect_heads(person_session, img, threshold=PERSON_THRESHOLD)
                  if (b[3] - b[1]) >= 3 * MIN_INVENTED_HEAD_PX * scale]
    if real is None or not found:
        return len(found)
    a = np.asarray(real) > 127
    count = 0
    for x0, y0, x1, y1 in found:
        patch = a[max(0, int(y0)):int(y1), max(0, int(x0)):int(x1)]
        if patch.size == 0 or patch.mean() < 0.3:
            count += 1
    return count


def clear_face(layout, cut, symbol, head_session, symbol128=None):
    """Keep the symbol off a behind-the-symbol character's face (at most
    MAX_FACE_COVERED of it): first shift the character sideways; if that
    isn't enough (a big portrait behind a centred symbol), move the symbol,
    smaller and to the clearest side. Returns the (possibly changed) layout;
    the caller re-places the symbol from layout['symbol']."""
    import numpy as np
    from PIL import Image
    sym = np.asarray(symbol) > 127

    def face_box(lay):
        layer = place_character(cut, lay, GEN_SIZE)
        flat = Image.new('RGB', layer.size, (128, 128, 128))
        flat.paste(layer, mask=layer.getchannel('A'))
        heads = detect_heads(head_session, flat)
        return tuple(int(v) for v in max(heads, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))) if heads else None

    def covered(lay, sym=sym):
        box = face_box(lay)
        if box is None:
            return 0.0
        x0, y0, x1, y1 = box
        face = sym[max(0, y0):y1, max(0, x0):x1]
        return float(face.mean()) if face.size else 0.0

    if covered(layout) <= MAX_FACE_COVERED:
        return layout
    height, cx, bottom = layout['character']
    ys, xs = np.where(sym)
    away = 1.0 if not xs.size or cx >= xs.mean() / GEN_SIZE else -1.0
    for step in (0.12, 0.24, 0.36):
        for direction in (away, -away):
            moved = dict(layout, character=(height, cx + direction * step, bottom))
            if covered(moved) <= MAX_FACE_COVERED:
                return moved
    box = face_box(layout)
    if symbol128 is not None and box is not None:
        fx, fy = (box[0] + box[2]) / 2 / GEN_SIZE, (box[1] + box[3]) / 2 / GEN_SIZE
        scale = min(layout['symbol'][0], 0.7)
        room = (1 - scale) / 2
        spots = [(sx * room, sy * room) for sx in (-1, 0, 1) for sy in (-1, 0, 1) if (sx, sy) != (0, 0)]
        # Farthest from the face first.
        spots.sort(key=lambda d: -((0.5 + d[0] - fx) ** 2 + (0.5 + d[1] - fy) ** 2))
        for dx, dy in spots:
            moved = dict(layout, symbol=(scale, dx, dy))
            if covered(moved, np.asarray(place_symbol(symbol128, moved, GEN_SIZE)) > 127) <= MAX_FACE_COVERED:
                return moved
    return layout


def anchor_cut_sides(layout, cut, sides):
    """Move a cut figure so its cut sides run off the icon's edge, like a
    portrait photo, instead of ending in a visible line inside the icon."""
    height, cx, bottom = layout['character']
    width = min(0.9, cut.width * height / max(1, cut.height))
    if sides.get('top') and sides.get('bottom'):
        height, bottom = max(height, 1.1), 1.05
    elif sides.get('top'):
        bottom = height - 0.03
    elif sides.get('bottom'):
        bottom = max(bottom, 1.04)
    if sides.get('left') and not sides.get('right'):
        cx = width / 2 - 0.03
    elif sides.get('right') and not sides.get('left'):
        cx = 1 - width / 2 + 0.03
    return dict(layout, character=(height, cx, bottom))


def fade_cut_sides(cut, sides, fraction=0.22):
    """Fade the alpha towards each cut side over `fraction` of the cut-out."""
    import numpy as np
    from PIL import Image
    alpha = np.asarray(cut.getchannel('A'), dtype=np.float32)
    h, w = alpha.shape
    # 1.0 away from the edge, falling to 0 over the last `fraction` towards it.
    ramp = lambda n: np.clip(np.arange(n)[::-1] / max(1.0, n * fraction), 0, 1)
    if sides.get('bottom'):
        alpha *= ramp(h)[:, None]
    if sides.get('top'):
        alpha *= ramp(h)[::-1][:, None]
    if sides.get('right'):
        alpha *= ramp(w)[None, :]
    if sides.get('left'):
        alpha *= ramp(w)[::-1][None, :]
    out = cut.copy()
    out.putalpha(Image.fromarray(alpha.astype(np.uint8), 'L'))
    return out


def place_character(cut, layout, size):
    """RGBA layer (size x size) with the cut-out placed for the layout."""
    from PIL import Image
    height, cx, bottom = layout['character']
    h = int(height * size)
    w = int(cut.width * h / cut.height)
    if w > 0.9 * size:  # very wide cut-outs: fit by width instead
        w = int(0.9 * size)
        h = int(cut.height * w / cut.width)
    sized = cut.resize((max(1, w), max(1, h)), Image.Resampling.LANCZOS)
    if layout.get('dim'):
        dark = Image.new('RGBA', sized.size, (0, 0, 0, int(255 * layout['dim'])))
        dark.putalpha(Image.eval(sized.getchannel('A'), lambda v: int(v * layout['dim'])))
        sized = Image.alpha_composite(sized, dark)
    layer = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    # Keep the figure inside the icon's left, right and top edges (a layout or
    # a sideways shift off the symbol could push it past them and slice it);
    # only the bottom may run off, like a portrait.
    x = min(max(0, int(cx * size - w / 2)), size - w)
    y = max(0, int(bottom * size - h))
    layer.paste(sized, (x, y), sized)
    return layer


def outline_layer(layer, color, width):
    """Dark sticker outline around a layer's opaque pixels."""
    from PIL import Image, ImageFilter
    ring = layer.getchannel('A').filter(ImageFilter.MaxFilter(width * 2 + 1))
    out = Image.new('RGBA', layer.size, color + (0,))
    out.putalpha(ring)
    return Image.alpha_composite(out, layer)


# --- Symbol ------------------------------------------------------------------------
def symbol_mask(template_path, icon_name=None):
    """Soft 128px mask of the template's white symbol (template backgrounds are
    blue, so the darkest channel separates white from background, anti-aliasing
    included). With icon_name, its text area is left out (typeset separately)."""
    import numpy as np
    from PIL import Image
    rgb = np.asarray(load_rgb(template_path, (ICON_SIZE, ICON_SIZE)), dtype=np.float32)
    whiteness = rgb.min(axis=2)
    alpha = np.clip((whiteness - 60.0) / 150.0, 0.0, 1.0)
    if icon_name in TEXT_REGIONS:
        x0, y0, x1, y1 = TEXT_REGIONS[icon_name][0]
        alpha[y0:y1, x0:x1] = 0
    return Image.fromarray((alpha * 255).astype(np.uint8), 'L')


def place_symbol(mask128, layout, size):
    """The symbol mask scaled/offset for the layout, on a size x size canvas."""
    from PIL import Image
    scale, dx, dy = layout['symbol']
    side = max(1, int(size * scale))
    sized = mask128.resize((side, side), Image.Resampling.LANCZOS)
    canvas = Image.new('L', (size, size), 0)
    canvas.paste(sized, (int((size - side) / 2 + dx * size), int((size - side) / 2 + dy * size)))
    return canvas


def place_box(box, layout):
    """A 128px template box moved/scaled with the symbol."""
    scale, dx, dy = layout['symbol']
    c = ICON_SIZE / 2
    x0, y0, x1, y1 = box
    move = lambda v, d: int(round((v - c) * scale + c + d * ICON_SIZE))
    return move(x0, dx), move(y0, dy), move(x1, dx), move(y1, dy)


def control_edges(*masks):
    from PIL import Image, ImageChops, ImageFilter
    edges = None
    for mask in masks:
        e = mask.point(lambda v: 255 if v > 127 else 0).filter(ImageFilter.FIND_EDGES).filter(ImageFilter.MaxFilter(3))
        edges = e if edges is None else ImageChops.lighter(edges, e)
    return Image.merge('RGB', (edges, edges, edges))


def outline_overlap(a, b):
    """Intersection-over-union of two shapes' outlines (edges grown a little)."""
    import numpy as np
    from PIL import ImageFilter
    def edges(m):
        return np.asarray(m.point(lambda v: 255 if v > 127 else 0).filter(ImageFilter.FIND_EDGES).filter(ImageFilter.MaxFilter(5))) > 127
    ea, eb = edges(a), edges(b)
    union = (ea | eb).sum()
    return float((ea & eb).sum() / union) if union else 0.0


def symbol_overlap(drawn, template):
    """Intersection-over-union of the drawn symbol and the template shape."""
    import numpy as np
    a, b = np.asarray(drawn) > 127, np.asarray(template) > 127
    union = (a | b).sum()
    return float((a & b).sum() / union) if union else 0.0


def has_inner_features(shape, min_share=0.03):
    """True if a design shape has holes inside it (eyes, a keyhole, a slot):
    background pixels not connected to the outside of the shape."""
    import numpy as np
    from PIL import Image, ImageDraw
    filled = np.asarray(shape.point(lambda v: 255 if v > 127 else 0).convert('L'))
    holes = ((filled == 0) & ~border_region(filled)).sum()
    body = (filled > 0).sum()
    return bool(body) and holes / body >= min_share


def feature_contrast(drawing, shape):
    """How clearly a drawing still shows its design's inner features: the
    brightness gap between the holes (eyes, a keyhole) and the body right
    around them. Overall variation wasn't enough: a bevel's shading passed
    while the eyes and smiles had washed out to near white."""
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter
    filled = shape.point(lambda v: 255 if v > 127 else 0).convert('L')
    f = np.asarray(filled)
    holes = Image.fromarray((((f == 0) & ~border_region(f)) * 255).astype(np.uint8), 'L')
    reach = 2 * max(2, shape.width // 64) + 1
    around = (np.asarray(rank_filter(holes, reach)) > 0) & (f > 0)
    inner = np.asarray(holes.filter(ImageFilter.MinFilter(3))) > 0
    if not inner.any() or not around.any():
        return 0.0
    lum = np.asarray(drawing.convert('L'), dtype=np.float32)
    return float(abs(np.median(lum[around]) - np.median(lum[inner])))


def smooth_under(img, area):
    """The image with `area` ('L') filled in smoothly from the colours around
    it (normalised blur), blended in with a soft edge."""
    import numpy as np
    from PIL import Image, ImageFilter
    hole = np.asarray(area, dtype=np.float32) / 255
    keep = 1 - hole
    rgb = np.asarray(img.convert('RGB'), dtype=np.float32)
    radius = max(4, img.width // 12)
    num = np.asarray(Image.fromarray(np.clip(rgb * keep[..., None], 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)
    den = np.asarray(Image.fromarray((keep * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius)), dtype=np.float32)[..., None] / 255
    fill = np.where(den > 0.02, num / np.maximum(den, 0.02), rgb)
    soft = np.asarray(area.filter(ImageFilter.GaussianBlur(2)), dtype=np.float32)[..., None] / 255
    return Image.fromarray(np.clip(rgb * (1 - soft) + fill * soft, 0, 255).astype(np.uint8))


def apply_symbol(img, symbol, clarity, color, art=None):
    """Draw the symbol crisply from its design shape (the template, or how a
    community theme drew this app): a soft dark halo for contrast, an accent
    fill and a crisp dark outline. Guessing the shape back out of the AI's
    painting picked up highlights and clothing as "symbol", so the shape the
    AI was guided by is the one drawn."""
    from PIL import Image, ImageChops, ImageFilter
    if clarity <= 0:
        return img
    if art is not None:
        # The background was painted with the symbol's shape in place, so it
        # holds a faint imprint of it. The drawn symbol never lines up with it
        # exactly, and the imprint showed beside it and through its gaps as a
        # doubled, shaken-looking copy: smooth it into the surrounding
        # background first (only under the symbol; the rest is untouched).
        # The symbol's enclosed openings count too: the background behind them
        # often holds a blurry copy of the symbol's motif (a second play button
        # inside the video frame). They stay see-through, just without it.
        import numpy as np
        drawn_on = (np.asarray(art.getchannel('A')) > 127).astype(np.uint8) * 255
        openings = Image.fromarray((((drawn_on == 0) & ~border_region(drawn_on)) * 255).astype(np.uint8), 'L')
        under = ImageChops.lighter(ImageChops.lighter(rank_filter(symbol, 5), rank_filter(art.getchannel('A'), 5)), openings)
        img = smooth_under(img, under)
        # The AI-drawn symbol: its own silhouette and details, not the flat shape.
        symbol = art.getchannel('A')
    halo = rank_filter(symbol, 7).filter(ImageFilter.GaussianBlur(5))
    img = Image.composite(Image.new('RGB', img.size, (0, 0, 0)), img, halo.point(lambda v: int(v * 0.55 * clarity)))
    if art is not None:
        img = Image.alpha_composite(img.convert('RGBA'), art).convert('RGB')
    else:
        # Shaded top to bottom like an embossed emblem: a single flat fill read
        # as a paint-bucket blob and hid the design's details.
        box = symbol.getbbox() or (0, 0, img.width, img.height)
        shade = Image.new('L', (1, img.height))
        for y in range(img.height):
            t = min(1.0, max(0.0, (y - box[1]) / max(1, box[3] - box[1])))
            shade.putpixel((0, y), int(255 * (1.12 - 0.42 * t) / 1.12))
        shade = shade.resize(img.size)
        light = Image.new('RGB', img.size, tuple(min(255, int(c * 1.12 + 12)) for c in color))
        fill = ImageChops.multiply(light, Image.merge('RGB', (shade, shade, shade)))
        # Fully opaque: partly see-through it looked ghosted.
        img = Image.composite(fill, img, symbol)
    ring = ImageChops.subtract(symbol.filter(ImageFilter.MaxFilter(3)), symbol)
    # A thin dark outline, or a light one around a black symbol so it shows.
    outline = tuple(int(c * 0.12) for c in color) if _luminance(color) > 0.08 else (225, 225, 225)
    return Image.composite(Image.new('RGB', img.size, outline), img, ring.point(lambda v: int(v * min(1.0, 0.5 + clarity))))


def stamp_text(icon, icon_name, color, box):
    """Typeset the icon's text with a real font so it is always crisp and readable.

    Uses Pillow's built-in scalable font (same on every OS), drawn at 4x and
    downsampled for smooth edges."""
    from PIL import Image, ImageDraw, ImageFont
    region = TEXT_REGIONS.get(icon_name)
    if not region:
        return icon
    _box, mode, text = region
    x0, y0, x1, y1 = box
    scale = 4
    w, h = (x1 - x0) * scale, (y1 - y0) * scale
    if w < 8 or h < 8:
        return icon
    dark = tuple(int(c * 0.12) for c in color)
    layer = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    if mode == 'panel':
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=4 * scale, fill=color + (255,), outline=dark + (255,), width=scale)
        fill, outline_width = dark, 0
    else:
        draw.rounded_rectangle((0, 0, w - 1, h - 1), radius=h // 2, fill=(0, 0, 0, 140))
        fill, outline_width = color, int(1.5 * scale)
    # Largest size that fits the area.
    size = h
    while size > 8:
        font = ImageFont.load_default(size=size)
        left, top, right, bottom = draw.textbbox((0, 0), text, font=font, stroke_width=outline_width)
        if right - left <= w * 0.88 and bottom - top <= h * 0.82:
            break
        size -= 2
    pos = ((w - (right - left)) / 2 - left, (h - (bottom - top)) / 2 - top)
    if outline_width:
        draw.text(pos, text, font=font, fill=fill + (255,), stroke_width=outline_width, stroke_fill=dark + (255,))
    # A thin same-colour stroke gives the regular-weight font a bold, icon-like weight.
    draw.text(pos, text, font=font, fill=fill + (255,), stroke_width=max(1, scale // 2), stroke_fill=fill + (255,))
    layer = layer.resize((x1 - x0, y1 - y0), Image.Resampling.LANCZOS)
    out = icon.convert('RGBA')
    out.alpha_composite(layer, (x0, y0))
    return out.convert('RGB')


# --- Layouts learned from example icon sets -------------------------------------
def read_example_layout(session, example_path, template_path):
    """Where the character and symbol sit in one example icon, as a layout."""
    import numpy as np
    from PIL import Image, ImageFilter
    img = load_rgb(example_path, (ICON_SIZE, ICON_SIZE))
    rgb = np.asarray(img, dtype=np.float32)
    char = segment(session, img) > 0.5
    if char.mean() < 0.04:
        char[:] = False
    near_char = np.asarray(Image.fromarray(char.astype(np.uint8) * 255).filter(ImageFilter.MaxFilter(5))) > 0
    symbol = (rgb.min(axis=2) > 185) & (rgb.max(axis=2) - rgb.min(axis=2) < 60) & ~near_char
    if symbol.mean() < 0.01:
        return None
    template = np.asarray(symbol_mask(template_path)) > 127
    if not template.any():
        return None
    box = lambda m: (np.where(m.any(axis=0))[0][[0, -1]], np.where(m.any(axis=1))[0][[0, -1]])
    (sx0, sx1), (sy0, sy1) = box(symbol)
    (tx0, tx1), (ty0, ty1) = box(template)
    scale = float(np.clip(((sx1 - sx0) / max(1, tx1 - tx0) + (sy1 - sy0) / max(1, ty1 - ty0)) / 2, MIN_SYMBOL_SCALE, 1.1))
    c = ICON_SIZE / 2
    dx = ((sx0 + sx1) / 2 - c - ((tx0 + tx1) / 2 - c) * scale) / ICON_SIZE
    dy = ((sy0 + sy1) / 2 - c - ((ty0 + ty1) / 2 - c) * scale) / ICON_SIZE
    layout = dict(symbol=(round(scale, 3), round(float(dx), 3), round(float(dy), 3)), character=None, front=False)
    if char.any():
        (cx0, cx1), (cy0, cy1) = box(char)
        overlap = char[sy0:sy1 + 1, sx0:sx1 + 1].mean()
        layout['character'] = (round((cy1 - cy0 + 1) / ICON_SIZE, 3), round((cx0 + cx1 + 1) / 2 / ICON_SIZE, 3),
                               round((cy1 + 1) / ICON_SIZE, 3))
        # Covering a good part of the symbol's area means it was drawn in front.
        layout['front'] = bool(overlap > 0.15)
    return layout


def example_sets(examples_dirs):
    """(set name, folder) of every example icon set, each set once: the app's
    built-in copy of a community set wins over a downloaded one."""
    seen, sets = set(), []
    for root in examples_dirs or []:
        if not root or not os.path.isdir(root):
            continue
        for set_name in sorted(os.listdir(root)):
            set_dir = os.path.join(root, set_name)
            if os.path.isdir(set_dir) and set_name not in seen:
                seen.add(set_name)
                sets.append((set_name, set_dir))
    return sets


def example_layouts(session, examples_dirs, templates_dir, cache_dir):
    """{icon_name: [layout, ...]} from every example set, cached by file size/time."""
    cache_path = os.path.join(cache_dir, 'layout_cache.json') if cache_dir else None
    cache = {}
    if cache_path and os.path.isfile(cache_path):
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                cache = json.load(f)
            if cache.get('version') != LAYOUT_CACHE_VERSION:
                cache = {}
        except (OSError, ValueError):
            cache = {}
    entries = cache.setdefault('entries', {})
    cache['version'] = LAYOUT_CACHE_VERSION
    layouts = {}
    for set_name, set_dir in example_sets(examples_dirs):
        for icon_name in ICON_SUBJECTS:
            path = os.path.join(set_dir, icon_name)
            if not os.path.isfile(path):
                continue
            st = os.stat(path)
            key = f'{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}'
            if key not in entries:
                entries[key] = read_example_layout(session, path, os.path.join(templates_dir, icon_name))
            if entries[key]:
                layouts.setdefault(icon_name, []).append(entries[key])
    if cache_path:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_path, 'w', encoding='utf-8') as f:
                json.dump(cache, f)
        except OSError:
            pass
    return layouts


# How a symbol-only icon is composed (chosen by the seed): the centred
# medallion of the user's favourite icon is one option among several, so
# placements differ between icons and generations instead of always sitting
# big in the middle. name -> (weight, scale range, x reach, y reach); reaches
# are shares of the free room around the symbol (signs picked by the seed).
# Symbols stay big (75%+): small or corner symbols left large empty areas that
# the model filled with smeared streaks (A/B tested on Elfen Lied).
EMBLEM_COMPOSITIONS = {
    'medallion': (0.40, (0.85, 1.00), (0.0, 0.0), (0.0, 0.0)),
    'side':      (0.25, (0.75, 0.88), (0.6, 1.0), (0.0, 0.3)),
    'low':       (0.175, (0.75, 0.88), (0.0, 0.3), (0.6, 1.0)),
    'high':      (0.175, (0.75, 0.88), (0.0, 0.3), (0.6, 1.0)),
}
# Icons with typeset text keep the text readable: big symbol, small moves only.
TEXT_COMPOSITIONS = {
    'medallion': (0.6, (0.88, 1.00), (0.0, 0.0), (0.0, 0.0)),
    'nudged':    (0.4, (0.80, 0.88), (0.5, 0.9), (0.5, 0.9)),
}


def compose_emblem(layout, rng, text=False):
    """A seeded composition for a symbol-only layout (see EMBLEM_COMPOSITIONS)."""
    options = TEXT_COMPOSITIONS if text else EMBLEM_COMPOSITIONS
    name = rng.choices(list(options), weights=[w for w, *_ in options.values()])[0]
    _w, (lo, hi), (xr0, xr1), (yr0, yr1) = options[name]
    scale = rng.uniform(lo, hi)
    room = max(0.0, (1 - scale) / 2)
    sign = lambda: rng.choice((-1, 1))
    dx = sign() * rng.uniform(xr0, xr1) * room
    dy = rng.uniform(yr0, yr1) * room
    dy = dy if name == 'low' else -dy if name == 'high' else sign() * dy
    return dict(layout, symbol=(scale, dx, dy), composition=name)


def pick_design(options, template, rng, exclude=()):
    """A community design for the symbol, favouring distinctive ones: every
    design here already reads as the app (consensus-checked), so the ones
    least like the stock template are picked more often."""
    options = [o for o in options if o[1] not in exclude]
    if not options:
        return None, None
    weights = [0.6 + (1 - symbol_overlap(fit_symbol(m, template), template)) for m, _p in options]
    return rng.choices(options, weights=weights)[0]


def vary_layout(layout, rng, text=False):
    """Seeded per-icon variation of a layout, so positions differ between icons
    and between generations: mirror the character to the other side (the art
    itself is never flipped), nudge the symbol and character, keep the symbol
    readable and fully inside the icon."""
    if layout.get('emblem'):
        return compose_emblem(layout, rng, text)
    scale, dx, dy = layout['symbol']
    char = layout['character']
    mirror = char is not None and rng.random() < 0.5
    low = MIN_SYMBOL_SCALE if char else 0.68 if layout.get('scene') else 0.8
    scale = min(1.05, max(low, scale * rng.uniform(0.9, 1.1)))
    room = max(0.0, (1 - scale) / 2)
    clamp = lambda v: max(-room, min(room, v))
    if layout.get('scene'):
        # A scene's symbol can sit anywhere in the environment (a sign in the
        # landscape), not only the centre.
        dx, dy = rng.uniform(-1, 1) * room, rng.uniform(-1, 1) * room
    dx = clamp((-dx if mirror else dx) + rng.uniform(-0.05, 0.05))
    dy = clamp(dy + rng.uniform(-0.05, 0.05))
    varied = dict(layout, symbol=(scale, dx, dy))
    if char:
        height, cx, bottom = char
        varied['character'] = (height * rng.uniform(0.88, 1.1),
                               (1 - cx if mirror else cx) + rng.uniform(-0.05, 0.05),
                               bottom + rng.uniform(-0.03, 0.04))
    return varied


# --- Symbol designs from example icon sets ------------------------------------------
EXAMPLE_SYMBOL_CHANCE = 0.85   # otherwise the built-in template symbol is used
# Other authors' versions of the same icon added as style references (on top
# of the one design whose outline is followed): ideas from several sets
# without blending their outlines into a muddled shape.
EXAMPLE_STYLE_REFS = 2


def example_symbol(path):
    """The symbol from an example icon drawn on a plain background, as a soft
    128px mask, or None for busy backgrounds / pictures without a symbol."""
    import numpy as np
    from PIL import Image, ImageFilter
    a = np.asarray(load_rgb(path, (ICON_SIZE, ICON_SIZE)), dtype=np.float32)
    border = np.concatenate([a[0], a[-1], a[:, 0], a[:, -1]])
    bg = np.median(border, axis=0)
    if np.sqrt(((border - bg) ** 2).sum(axis=1)).mean() > 25:
        return None  # not a plain background
    diff = np.sqrt(((a - bg) ** 2).sum(axis=2))
    core = Image.fromarray(((diff > 60) * 255).astype(np.uint8), 'L').filter(ImageFilter.MinFilter(3)).filter(ImageFilter.MaxFilter(3))
    coverage = (np.asarray(core) > 127).mean()
    if not 0.03 <= coverage <= 0.6:
        return None
    shape = Image.fromarray((((diff > 60) & (np.asarray(core.filter(ImageFilter.MaxFilter(5))) > 0)) * 255).astype(np.uint8), 'L')
    # Smooth the jagged edges: blur at 4x, re-threshold, scale back down antialiased.
    big = shape.resize((ICON_SIZE * 4, ICON_SIZE * 4), Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(3))
    big = big.point(lambda v: 255 if v > 127 else 0)
    return big.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)


def fit_symbol(symbol, template):
    """Scale/move an example symbol onto the template symbol's position, so
    layouts place it exactly like the template. It is sized to the template's
    visual weight (filled area), not its box: a solid camera stretched over the
    photos template's wide, airy box looked far too big. It never exceeds the
    template's box."""
    import numpy as np
    from PIL import Image
    sb, tb = symbol.getbbox(), template.getbbox()
    if not sb or not tb:
        return template
    piece = symbol.crop(sb)
    box_scale = min((tb[2] - tb[0]) / piece.width, (tb[3] - tb[1]) / piece.height)
    piece_area = float((np.asarray(piece) > 127).sum())
    template_area = float((np.asarray(template) > 127).sum())
    area_scale = (template_area / piece_area) ** 0.5 if piece_area and template_area else box_scale
    scale = min(box_scale, area_scale)
    piece = piece.resize((max(1, int(piece.width * scale)), max(1, int(piece.height * scale))), Image.Resampling.LANCZOS)
    canvas = Image.new('L', template.size, 0)
    cx, cy = (tb[0] + tb[2]) / 2, (tb[1] + tb[3]) / 2
    canvas.paste(piece, (int(cx - piece.width / 2), int(cy - piece.height / 2)))
    return canvas


# A design is kept only if it clearly resembles another drawing of the same app.
# Shapes are compared after fitting to the same box, where any compact shape
# overlaps another by ~50%, so the bars are well above that.
SYMBOL_TEMPLATE_IOU = 0.5    # resembles the template, or
SYMBOL_CLOSE_IOU = 0.75      # closely matches one other theme's design, or
SYMBOL_AGREE_IOU = 0.6       # fairly matches at least two other themes' designs
SYMBOL_SAME_IOU = 0.9     # designs this alike count once
SYMBOL_MAX_FILL = 0.8     # solid blobs (filling their box) are extraction leftovers, not designs
SYMBOL_MAX_SPECKS = 6     # designs breaking into many tiny fragments are noise, not designs
# Filled-area overlap is fooled by blobs (the friends template is two solid
# squares, so any blob "matches" it); outlines (edges, eyes, gaps) are not.
SYMBOL_OUTLINE_TEMPLATE = 0.35  # outline overlap with the template, or
SYMBOL_OUTLINE_OTHER = 0.6      # with another theme's design for the same app


def example_symbols(examples_dirs, templates_dir):
    """{icon_name: [(symbol mask, example icon path), ...]} from every example set.

    A design is only kept if it clearly resembles another drawing of the same
    app (the template, or other themes' designs; see SYMBOL_*_IOU): real symbols recur across themes,
    while frames, silhouettes and decorations lifted by mistake don't.
    Near-identical designs are kept once. Icons with typeset text keep the
    template (their text is redrawn exactly)."""
    import numpy as np
    from PIL import Image, ImageChops, ImageFilter
    found = {}
    for set_name, set_dir in example_sets(examples_dirs):
        symbols = {}
        for icon_name in ICON_SUBJECTS:
            path = os.path.join(set_dir, icon_name)
            if os.path.isfile(path):
                symbol = example_symbol(path)
                if symbol is not None:
                    symbols[icon_name] = (symbol, path)
        # Picture-led sets (portraits, photos) are layout references only.
        if len(symbols) < len(ICON_SUBJECTS) // 2:
            continue
        # Decoration repeated on most icons of the set (a frame, wings...)
        # is not part of any symbol: subtract it. Symbols share the centre
        # of the icon, so only repeated pixels outside the centre count.
        stack = np.stack([np.asarray(m) > 127 for m, _p in symbols.values()])
        repeated = stack.mean(axis=0) > 0.6
        c0, c1 = ICON_SIZE // 4, ICON_SIZE * 3 // 4
        repeated[c0:c1, c0:c1] = False
        frame = Image.fromarray((repeated * 255).astype(np.uint8), 'L').filter(ImageFilter.MaxFilter(3))
        for icon_name, (symbol, path) in symbols.items():
            if icon_name in TEXT_REGIONS:
                continue
            symbol = ImageChops.subtract(symbol, frame)
            if (np.asarray(symbol) > 127).mean() >= 0.02:
                found.setdefault(icon_name, []).append((symbol, path))
    agreed = {}
    for icon_name, candidates in found.items():
        template = symbol_mask(os.path.join(templates_dir, icon_name))
        fitted = [fit_symbol(m, template) for m, _p in candidates]
        keep = []
        for i, mask in enumerate(fitted):
            box = mask.getbbox()
            if box and (np.asarray(mask.crop(box)) > 127).mean() > SYMBOL_MAX_FILL:
                continue
            specks = sum(1 for area, _cells in _components(np.asarray(mask) > 127) if area < 12)
            if specks > SYMBOL_MAX_SPECKS:
                continue
            if not (outline_overlap(mask, template) >= SYMBOL_OUTLINE_TEMPLATE
                    or any(outline_overlap(mask, f) >= SYMBOL_OUTLINE_OTHER for j, f in enumerate(fitted) if j != i)):
                continue
            overlaps = [symbol_overlap(mask, f) for j, f in enumerate(fitted) if j != i]
            if not (symbol_overlap(mask, template) >= SYMBOL_TEMPLATE_IOU
                    or any(o >= SYMBOL_CLOSE_IOU for o in overlaps)
                    or sum(o >= SYMBOL_AGREE_IOU for o in overlaps) >= 2):
                continue
            if any(symbol_overlap(mask, fitted[k]) > SYMBOL_SAME_IOU for k in keep):
                continue
            keep.append(i)
        if keep:
            agreed[icon_name] = [candidates[i] for i in keep]
    return agreed


# Which apps feature a character is a fixed rule (the same for every theme):
# social and media apps get one, system and utility apps are symbol-only and
# just fit the theme. The seed only varies how each icon looks.
CHARACTER_ICONS = {
    'icon_friend.png', 'icon_party.png', 'icon_message.png', 'icon_near.png',
    'icon_music.png', 'icon_photos.png', 'icon_video.png', 'icon_trophy.png',
}


def wants_character(icon_name, style, has_characters):
    return has_characters and style != 'emblem' and icon_name in CHARACTER_ICONS


def pick_layouts(style, icons, has_characters, rng):
    """One layout per icon: 'emblem' for symbol-only icons, and for character
    icons the style's character layouts in shuffled cycles, so no layout
    repeats twice in a row."""
    names = [n for n in LAYOUT_STYLES.get(style, LAYOUT_STYLES['mixed']) if n != 'emblem'] or ['peek']
    picks, cycle = [], []
    for icon_name in icons:
        if not wants_character(icon_name, style, has_characters):
            picks.append('emblem')
            continue
        if not cycle:
            cycle = names[:]
            rng.shuffle(cycle)
            last = next((p for p in reversed(picks) if p != 'emblem'), None)
            if len(cycle) > 1 and cycle[0] == last:
                cycle.append(cycle.pop(0))
        picks.append(cycle.pop(0))
    return picks


def _flux2_full(variant):
    """True for the full bf16 build (the faster, high-end-GPU, larger download);
    False for the 4-bit build (slower, runs on ~8-12 GB cards)."""
    return FLUX2_VARIANTS.get(variant, {}).get('quant') == 'bf16'


def flux2_files(variant='flux2'):
    """The FLUX.2 download for a variant, as (repo, allow_patterns). The full
    build is everything from the base repo (bf16, a gated repo — needs a Hugging
    Face account + licence acceptance + token). The 4-bit build is self-contained
    in the ungated diffusers repo (transformer + text encoder + VAE), so it needs
    no account."""
    if _flux2_full(variant):
        return [(FLUX2_REPO, ['transformer/*', 'text_encoder/*', 'vae/*', 'scheduler/*', 'tokenizer*/*', 'model_index.json'])]
    return [(FLUX2_BNB_REPO, ['transformer/*', 'text_encoder/*', 'vae/*', 'scheduler/*', 'tokenizer*/*', 'model_index.json'])]


def flux2_key_files(variant='flux2'):
    """A few (repo, path) files that mark the variant as downloaded."""
    repo = FLUX2_REPO if _flux2_full(variant) else FLUX2_BNB_REPO
    return [(repo, 'transformer/config.json'), (repo, 'text_encoder/config.json'), (repo, 'vae/config.json')]


def flux2_present(variant='flux2'):
    """True when the FLUX.2 variant looks downloaded (key configs cached)."""
    from huggingface_hub import try_to_load_from_cache
    return all(isinstance(try_to_load_from_cache(r, p), str) for r, p in flux2_key_files(variant))


def ensure_flux2(variant='flux2', token=None):
    """Download the FLUX.2 variant if it isn't cached yet (progress via events).
    The full build is a gated repo: `token` (a Hugging Face token, from the app's
    settings) is required for it. Passed explicitly, not via the environment."""
    if flux2_present(variant):
        return
    from huggingface_hub import snapshot_download
    for repo, patterns in flux2_files(variant):
        emit('status', message=f'Downloading the FLUX.2 model ({repo})...')
        try:
            snapshot_download(repo, allow_patterns=patterns, token=token or None)
        except Exception as e:
            if _flux2_full(variant):
                raise RuntimeError(
                    "Could not download the full FLUX.2 model — it is a gated model. Create a Hugging Face "
                    "token, accept the FLUX.2 licence on its model page, and paste the token in "
                    "Settings > AI Icons (Hugging Face token).") from e
            raise


def _flux2_ref(path, size=FLUX2_REF_SIZE):
    """A reference image loaded as RGB and capped to `size` on its long side."""
    from PIL import Image
    img = Image.open(path).convert('RGB')
    if max(img.size) > size:
        img.thumbnail((size, size), Image.Resampling.LANCZOS)
    return img


def flux2_theme_analysis(wallpapers):
    """A local description of the theme to guide FLUX.2, built from the same
    tools the scaffold uses: art style, recurring scenery/motifs, the theme's
    iconic features (horns, a microphone, a character's name) and its colour
    palette. Richer prompts are where a smart model earns its keep, so this
    stands in for the Gemini path's cloud analysis, locally."""
    analysis = {}
    try:
        style = detect_art_style(wallpapers)
        analysis['style_words'] = ART_STYLES.get(style, ('', ''))[0]
    except Exception:
        analysis['style_words'] = ''
    try:
        motifs, traits = theme_tags(wallpapers)
        analysis['setting'] = ', '.join(motifs[:6])
        analysis['traits'] = ', '.join(traits)   # dict keys: the iconic features, strongest first
    except Exception:
        analysis['setting'] = analysis['traits'] = ''
    try:
        analysis['palette'] = ', '.join('#%02x%02x%02x' % c for c in palette_colors(wallpapers, 5))
    except Exception:
        analysis['palette'] = ''
    return analysis


def flux2_icon_prompt(name, job, analysis, roles):
    """A natural-language prompt for one icon, referencing the images by their
    position (FLUX.2 takes one prompt plus an ordered image list). Mirrors the
    Gemini recipe's rules and folds in the local theme analysis (style, setting,
    palette, iconic features): the symbol must stay recognisable, the art matches
    the theme, character icons feature a theme character, and text is spelled out."""
    subject = ICON_SUBJECTS.get(name, 'app')
    text = TEXT_REGIONS.get(name)
    mode = job.get('theme_color_mode', 'auto')
    colours = '' if mode == 'never' else ', '.join('#' + c[-6:] for c in job.get('theme_colors') or [] if len(c) >= 6)
    analysis = analysis or {}
    sym_i = roles['symbol']
    walls = roles['wallpapers']
    lines = [
        f"Create the PlayStation Vita '{subject}' app icon for the theme \"{job.get('title') or 'custom theme'}\".",
        f"Image {sym_i} is the app's system symbol: keep the result clearly recognisable as the {subject} symbol. "
        "You may redesign its shape and give it detail, colour and depth, but it must still read as that symbol.",
        f"Images {walls[0]}-{walls[-1]} are the theme's wallpapers: match their art style, colours, lighting and world. "
        "Don't copy a wallpaper; make new icon artwork that belongs to the theme." if len(walls) > 1 else
        (f"Image {walls[0]} is a theme wallpaper: match its art style, colours and world; don't copy it." if walls else ''),
    ]
    if analysis.get('style_words'):
        lines.append(f"Art style: {analysis['style_words']}.")
    if analysis.get('setting'):
        lines.append(f"The theme's world and recurring motifs: {analysis['setting']}.")
    if analysis.get('palette'):
        lines.append(f"The theme's colour palette: {analysis['palette']}. Build the icon from these colours.")
    if colours:
        lines.append(f"Use the theme's interface colours {colours} as the icon's main colours." if mode == 'always' else
                     f"The theme's interface colours are {colours}; use them where they suit the art.")
    if job.get('layout_style') != 'emblem' and name in CHARACTER_ICONS:
        trait_hint = (f" The theme's signature features include: {analysis['traits']} — keep whichever belong to the character."
                      if analysis.get('traits') else '')
        lines.append("Feature one main character from the theme, copied faithfully from the wallpaper references "
                     "(same face, hair, outfit and colours — do not invent a new person)." + trait_hint +
                     " Place them next to or interacting with the symbol; the symbol must stay clearly visible. "
                     "Never cut the character off at the left, right or top edge.")
    else:
        extra = f" Draw on the theme's motifs ({analysis['setting']})." if analysis.get('setting') else ''
        lines.append("No characters or people in this icon: build it from the theme's scenery, shapes and motifs." + extra)
    if roles.get('made'):
        m = roles['made']
        lines.append(f"Image{'s' if len(m) > 1 else ''} {', '.join(str(i) for i in m)} are icons already made for this "
                     "set: match their style and finish for a consistent set.")
    lines.append(f"Render the text `{text[2]}` clearly as part of the symbol, spelled exactly, in a clean font."
                 if text else "No text, letters or logos anywhere.")
    lines.append("Square, full-bleed artwork filling the whole image: no border, no rounded corners, no frame.")
    return '\n'.join(l for l in lines if l)


def run_reference(job):
    """The large-model 'reference' recipe (FLUX.2): a local counterpart to the
    Gemini path. Each icon is made from a prompt plus reference images (the
    symbol, the theme's wallpapers and the icons already made), letting the
    model handle fidelity itself instead of the SD 1.5/SDXL scaffolding."""
    import torch
    from PIL import Image
    from diffusers import Flux2Pipeline

    variant = job.get('model', 'flux2')
    full = _flux2_full(variant)
    icons = job['icons']
    wallpapers = [p for p in job['wallpapers'] if os.path.isfile(p)]
    if not wallpapers:
        raise ValueError('At least one wallpaper is required as a style reference.')
    os.makedirs(job['output_dir'], exist_ok=True)

    hf_token = (job.get('hf_token') or '').strip() or None
    emit('status', message='Checking the FLUX.2 model...')
    ensure_flux2(variant, token=hf_token)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    dtype = torch.bfloat16
    emit('status', message=f"Loading {'the full' if full else 'the 4-bit'} FLUX.2 model on {device}...")
    # Full bf16 build loads from the (gated) base repo (needs the token); the
    # 4-bit build is self-contained in the ungated diffusers repo (its config
    # carries the quantization, so from_pretrained loads it 4-bit automatically).
    pipe = Flux2Pipeline.from_pretrained(FLUX2_REPO if full else FLUX2_BNB_REPO, torch_dtype=dtype,
                                         token=hf_token if full else None)

    vram = torch.cuda.get_device_properties(0).total_memory if device == 'cuda' else 0
    force = os.environ.get('VTA_FORCE_OFFLOAD') == '1' or bool(job.get('min_memory'))
    # Offload strength is automatic, from the variant and the card's VRAM (and the
    # 'minimum memory' option, for when other apps need the GPU). Group offload
    # (leaf-level streaming) is the slow ~8 GB path; model offload (whole
    # components) is faster but needs more VRAM; .to(device) is fastest.
    if device == 'cuda':
        aggressive = force or (not full and vram < 16 * 1024 ** 3) or (full and vram < 48 * 1024 ** 3)
        if aggressive:
            try:
                pipe.transformer.enable_group_offload(onload_device=torch.device(device), offload_device=torch.device('cpu'),
                                                      offload_type='leaf_level', use_stream=True)
                pipe.enable_model_cpu_offload()
            except Exception:
                pipe.enable_model_cpu_offload()
        elif (not full and vram < 20 * 1024 ** 3):
            pipe.enable_model_cpu_offload()
        else:
            pipe = pipe.to(device)
        try:
            pipe.vae.enable_tiling()
        except Exception:
            pass
    pipe.set_progress_bar_config(disable=True)

    emit('status', message='Studying the theme (style, motifs, palette)...')
    analysis = flux2_theme_analysis(wallpapers)
    if analysis.get('setting') or analysis.get('traits'):
        print(f"theme: {analysis.get('style_words', '')}; motifs: {analysis.get('setting', '')}; "
              f"features: {analysis.get('traits', '')}", flush=True)
    wall_refs = [_flux2_ref(p) for p in wallpapers[:FLUX2_MAX_WALLPAPERS]]
    extra_refs = [_flux2_ref(p) for p in (p for p in (job.get('extra_images') or []) if os.path.isfile(p))][:FLUX2_MAX_EXTRAS]
    common = wall_refs + extra_refs

    seed = int(job.get('seed', 0))
    steps = int(job.get('steps') or FLUX2_STEPS)
    made = []   # paths of icons generated this run (for set consistency)
    peak = 0
    for index, name in enumerate(icons):
        emit('progress', name=name, index=index, total=len(icons))
        template = os.path.join(job['templates_dir'], name)
        if not os.path.isfile(template):
            emit('skipped', name=name, index=index, total=len(icons), reason='template not found')
            continue
        # Build the ordered reference list and remember each image's role (1-based
        # positions for the prompt): symbol, wallpapers, then icons already made.
        images = [_flux2_ref(template)]
        roles = {'symbol': 1, 'wallpapers': list(range(2, 2 + len(common)))}
        images += list(common)
        consistency = list(dict.fromkeys((made[:1] + made[-1:])))[:FLUX2_MAX_REFS - len(images)]
        if consistency:
            start = len(images) + 1
            images += [_flux2_ref(p) for p in consistency]
            roles['made'] = list(range(start, start + len(consistency)))
        images = images[:FLUX2_MAX_REFS]
        prompt = flux2_icon_prompt(name, job, analysis, roles)
        guidance = FLUX2_GUIDANCE if len(images) <= 2 else FLUX2_GUIDANCE_MULTI
        generator = torch.Generator(device='cpu').manual_seed(seed + index * 7919)
        out = pipe(prompt=prompt, image=images, num_inference_steps=steps, guidance_scale=guidance,
                   height=FLUX2_SIZE, width=FLUX2_SIZE, generator=generator).images[0]
        icon = out.convert('RGB').resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
        out_path = os.path.join(job['output_dir'], name)
        icon.save(out_path, 'PNG')
        made.append(out_path)
        if device == 'cuda':
            peak = max(peak, torch.cuda.max_memory_allocated())
        emit('icon', name=name, index=index, total=len(icons), path=out_path)

    if device == 'cuda':
        print(f"peak GPU memory: {peak / 1024 ** 3:.1f} GB ({variant})", flush=True)
    emit_peak(peak if device == 'cuda' else 0, variant)
    emit('done')


def run(job):
    # Each backend picks its generation recipe; large models will slot in here
    # with the 'reference' recipe without disturbing the scaffold path below.
    if backend_for(job).get('recipe') == 'reference':
        return run_reference(job)

    global GEN_SIZE
    import torch
    from PIL import Image, ImageChops, ImageDraw, ImageFilter
    from diffusers import ControlNetModel, StableDiffusionControlNetImg2ImgPipeline

    model = job.get('model', 'sd15')
    sdxl = model == 'sdxl'
    # A user's own checkpoint (SD 1.5/SDXL) to use instead of the default base,
    # loaded in place. The app only sets this when its architecture matches the
    # selected tier, so the ControlNet/IP-Adapter below still apply.
    custom_base = (job.get('custom_base_model') or '').strip() or None
    # A custom checkpoint may carry extra properties the app must respect.
    custom_info = inspect_custom_model(custom_base, model) if custom_base else {}
    if custom_info.get('inpaint'):
        raise ValueError("This looks like an inpainting model, which the icon generator can't use. "
                         "Choose a standard text-to-image checkpoint (SD 1.5 or SDXL).")
    if sdxl:
        GEN_SIZE = SDXL_SIZE
    # Generate at the custom model's native resolution when known (clamped to the
    # architecture's range and a multiple of 64).
    if custom_info.get('native_res'):
        cap = 1024 if sdxl else 768
        GEN_SIZE = max(384, min(int(custom_info['native_res']), cap))
        GEN_SIZE -= GEN_SIZE % 64
    # Quality/negative tags default to the anime set (Animagine). A realistic
    # custom model gets neutral tags instead; anime/unknown keep the defaults.
    quality_tag, negative_tag = SDXL_QUALITY, SDXL_NEGATIVE
    if custom_info.get('domain') == 'realistic':
        quality_tag = 'best quality, highly detailed, sharp focus'
        negative_tag = 'low quality, blurry, deformed, text, watermark'
    if custom_info:
        print(f"custom model: arch={model}, native_res={custom_info.get('native_res')}, "
              f"prediction={custom_info.get('prediction_type') or 'epsilon'}, zsnr={custom_info.get('zsnr')}, "
              f"domain={custom_info.get('domain') or 'unknown'}", flush=True)

    wallpapers = job['wallpapers'][:MAX_STYLE_REFERENCES]
    if not wallpapers:
        raise ValueError('At least one wallpaper is required as a style reference.')
    style = job.get('layout_style', 'mixed')

    device, dtype = pick_device()
    half = dtype == torch.float16
    emit('status', message='Checking AI models...')
    # AI poses: an OpenPose ControlNet for the chosen model (SD 1.5 or SDXL).
    pose_variation = bool(job.get('pose_variation')) and style != 'emblem'
    ensure_models(half, segmenter=style != 'emblem', poses=pose_variation, model=model, skip_base=bool(custom_base))

    characters, learned, same_as, figure_masks = [], {}, {}, {}
    trusted_community = []  # community theme pictures judged to be this theme's series and look
    text_detector = load_text_detector()
    head_detector = load_head_detector()
    face_detector = load_face_detector()
    person_detector = load_person_detector()
    art_style = detect_art_style(wallpapers)
    style_words, style_avoid = ART_STYLES[art_style]
    motifs, traits = theme_tags(wallpapers)
    print(f"theme motifs: {', '.join(motifs) or 'none found'}; iconic features: {', '.join(traits) or 'none found'}", flush=True)
    print(f'theme art style: {art_style} ({style_words})', flush=True)
    if style != 'emblem':
        session = load_segmenter()
        emit('status', message='Finding characters in the wallpapers...')
        # User-provided artwork adds characters and poses (not style: the
        # theme's look still comes from its wallpapers only).
        extras = [p for p in job.get('extra_images') or [] if os.path.isfile(p)]
        community = [p for p in job.get('auto_extra_images') or [] if os.path.isfile(p)]
        found = find_characters(session, head_detector, wallpapers + extras + community, figure_masks,
                                MAX_CHARACTERS_WITH_EXTRAS if extras or community else MAX_CHARACTERS)
        if community:
            # Judge every community picture before using it. Its characters are
            # kept only if they are clearly this theme's own (identity model).
            own = [c['head_crop'] for c in found if c.get('source') in wallpapers and c.get('head_crop') is not None]
            kept = [c for c in found if c.get('source') not in community or (
                c.get('head_crop') is not None and own
                and min(identity_diff(c['head_crop'], o) for o in own) <= COMMUNITY_ART_MATCH)]
            # A picture is also used as more style and scenery data only when it
            # shows one of this theme's characters AND is drawn in the same art
            # style: then it is very likely the same series and look.
            matched = {c['source'] for c in kept if c.get('source') in community}
            for path in community:
                chars = sum(1 for c in found if c.get('source') == path)
                good = sum(1 for c in kept if c.get('source') == path)
                same_style = art_style_scores([path]).get(art_style, 0.0) >= COMMUNITY_STYLE_MIN
                if path in matched and same_style:
                    trusted_community.append(path)
                use = ('characters + style and scenery' if path in trusted_community else
                       'characters only' if path in matched else 'not used')
                print(f'community art {os.path.basename(os.path.dirname(path))}/{os.path.basename(path)}: '
                      f'{good}/{chars} characters match, art style {"matches" if same_style else "differs"} -> {use}',
                      flush=True)
            found = kept
        same_as = same_character_groups(found)
        characters = [dict(c, image=strip_text(text_detector, c['image'])) for c in found]
        for c in characters:
            c['quality'] = character_quality(c, face_detector)
        if not characters:
            emit('status', message='No clear characters found in the wallpapers; making symbol-only icons.')
        if style in EXAMPLE_CHANCE and job.get('examples_dirs'):
            emit('status', message='Reading layouts from the example icon sets...')
            learned = example_layouts(session, job['examples_dirs'], job['templates_dir'], job.get('cache_dir'))

    emit('status', message=f"Loading {'the high-quality SDXL' if sdxl else 'the standard'} model on {device}...")
    style_scale = float(job.get('style_scale', 0.55))
    openpose = None
    if sdxl:
        from diffusers import StableDiffusionXLControlNetImg2ImgPipeline
        controlnet = ControlNetModel.from_pretrained(SDXL_CONTROLNET, torch_dtype=dtype, variant='fp16' if half else None)
        if custom_base:
            emit('status', message='Loading your custom SDXL model...')
            pipe = _load_base(StableDiffusionXLControlNetImg2ImgPipeline, custom_base, controlnet=controlnet, torch_dtype=dtype)
        else:
            pipe = StableDiffusionXLControlNetImg2ImgPipeline.from_pretrained(SDXL_MODEL, controlnet=controlnet, torch_dtype=dtype)
        if pose_variation:
            # Two adapters, as for SD 1.5: style for icons, 'plus' (a character's look) for pose redraws.
            pipe.load_ip_adapter(IP_ADAPTER_REPO, subfolder=['sdxl_models', 'sdxl_models'],
                                 weight_name=[SDXL_IP_WEIGHTS, SDXL_IP_PLUS_WEIGHTS], image_encoder_folder='models/image_encoder')
            pipe.set_ip_adapter_scale([style_scale, 0.0])
            # Kept on the CPU and swapped in only for a pose redraw: with it on
            # the GPU too, SDXL would no longer fit in 12 GB.
            openpose = ControlNetModel.from_pretrained(SDXL_OPENPOSE, torch_dtype=dtype)
        else:
            pipe.load_ip_adapter(IP_ADAPTER_REPO, subfolder='sdxl_models', weight_name=SDXL_IP_WEIGHTS,
                                 image_encoder_folder='models/image_encoder')
            pipe.set_ip_adapter_scale(style_scale)
        pipe.vae.enable_slicing()  # the pipeline-level helper no longer exists in current diffusers
    else:
        controlnet = ControlNetModel.from_pretrained(CONTROLNET_MODEL, torch_dtype=dtype, variant='fp16' if half else None)
        # The safety checker false-positives on abstract art and returns solid
        # black images, which is useless for icons.
        if custom_base:
            emit('status', message='Loading your custom SD 1.5 model...')
            pipe = _load_base(StableDiffusionControlNetImg2ImgPipeline, custom_base, controlnet=controlnet,
                              torch_dtype=dtype, safety_checker=None, requires_safety_checker=False)
        else:
            pipe = StableDiffusionControlNetImg2ImgPipeline.from_pretrained(
                BASE_MODEL,
                controlnet=controlnet,
                torch_dtype=dtype,
                variant='fp16' if half else None,
                safety_checker=None,
                requires_safety_checker=False,
            )
    if not sdxl and pose_variation:
        # Two adapters: style for icons, 'plus' (holds a character's look) for redraws.
        pipe.load_ip_adapter(IP_ADAPTER_REPO, subfolder=['models', 'models'], weight_name=[IP_ADAPTER_WEIGHTS, IP_ADAPTER_PLUS_WEIGHTS])
        pipe.set_ip_adapter_scale([style_scale, 0.0])
        openpose = ControlNetModel.from_pretrained(OPENPOSE_MODEL, torch_dtype=dtype, variant='fp16' if half else None).to(device)
    elif not sdxl:
        pipe.load_ip_adapter(IP_ADAPTER_REPO, subfolder='models', weight_name=IP_ADAPTER_WEIGHTS)
        pipe.set_ip_adapter_scale(style_scale)
    # Memory saving is automatic, chosen from the card's VRAM. The 'minimum
    # memory' option (for when other apps need the GPU/RAM) forces the least-
    # memory path. VTA_FORCE_OFFLOAD=1 tests the small-GPU path on a big card.
    vram = torch.cuda.get_device_properties(0).total_memory if device == 'cuda' else 0
    min_memory = bool(job.get('min_memory'))
    # SDXL with the pose model peaked at 13.8 GB on a 12 GB card (spilling into
    # system RAM, very slow): it needs the offload below 16 GB.
    small_gpu = device == 'cuda' and (vram < 10 * 1024 ** 3 or (pose_variation and vram < 16 * 1024 ** 3)
                                      or os.environ.get('VTA_FORCE_OFFLOAD') == '1')
    if device == 'cuda' and (min_memory or (vram and vram < 6 * 1024 ** 3)):
        # Minimum memory (or a very small card): stream layers one at a time and
        # tile the VAE, so the generator uses as little GPU memory as possible.
        pipe.enable_sequential_cpu_offload()
        try:
            pipe.enable_vae_tiling()
        except Exception:
            pass
    elif sdxl and small_gpu:
        # Keeps peak VRAM under 8 GB on smaller cards (slower: models move in and out).
        pipe.enable_model_cpu_offload()
    else:
        pipe = pipe.to(device)
    pipe.set_progress_bar_config(disable=True)
    # DPM++ 2M Karras matches the default sampler's quality in ~20 steps
    # instead of ~30: about a third faster on every pass, for both models.
    from diffusers import DPMSolverMultistepScheduler
    _sched_kw = {'use_karras_sigmas': True}
    if custom_info.get('prediction_type'):
        _sched_kw['prediction_type'] = custom_info['prediction_type']   # v-prediction models
    if custom_info.get('zsnr'):
        _sched_kw['rescale_betas_zero_snr'] = True
    pipe.scheduler = DPMSolverMultistepScheduler.from_config(pipe.scheduler.config, **_sched_kw)

    # The whole wallpaper pool is one multi-image reference for the single
    # IP-Adapter (nested list), so every icon shares the same theme style.
    style_refs = [scenery_reference(p, figure_masks.get(p), GEN_SIZE) for p in wallpapers + trusted_community]
    # Where scene icons take their environment from: the wallpapers and any
    # extra artwork (added files, web or Safebooru picks) that has a background.
    environments = wallpapers + trusted_community + [p for p in job.get('extra_images') or [] if os.path.isfile(p)]

    def clean_environment(rng):
        """A seeded environment crop with no characters, people or lettering
        in it (checked with the segmenter, head/person and text detectors),
        or None if the theme's pictures have none."""
        for source in rng.sample(environments, len(environments)):
            for coverage, crop in environment_crops(source, figure_masks.get(source), GEN_SIZE, rng)[:SCENE_CHECKS]:
                if coverage > SCENE_MAX_FIGURES:
                    break
                if invented_figures(head_detector, crop, None, person_detector):
                    continue
                if has_text(text_mask(text_detector, crop), SCENE_MAX_TEXT):
                    continue
                if scenery_detail(crop) < SCENE_MIN_DETAIL:
                    continue
                return crop
        return None
    neutral = Image.new('RGB', (GEN_SIZE, GEN_SIZE), (128, 128, 128))
    redraw_on = bool(job.get('redraw', True))
    accuracy = []  # identity differences of every redraw (lower = closer match)

    def adapter_images(refs):
        """Per-adapter reference images (the unused 'plus' adapter gets a neutral one)."""
        return [refs, [neutral]] if pose_variation else [refs]

    def draw_symbol_art(mask, subject, refs, rng, index, tries=SYMBOL_ART_TRIES, first_attempt=0, multipart=False,
                        trait=None):
        """The app's symbol drawn as artwork on a plain dark background, lifted
        out as RGBA (GEN_SIZE), or None if it no longer reads as the symbol."""
        import numpy as np
        # Own random picks: symbol drawing (and its retries) never shifts the
        # shared sequence that decides layouts and backgrounds.
        finish = random.Random(seed + index * 7919 + first_attempt + 17).choice(EMBLEM_FINISHES)
        canvas = Image.new('RGB', (GEN_SIZE, GEN_SIZE), (8, 8, 10))
        # An iconic item gets a rough silhouette next to the symbol (behind it),
        # in the starting image and the outline guide, so the model draws it.
        item = item_silhouette(trait, mask) if trait else None
        guide = ImageChops.lighter(mask, item) if item is not None else mask
        glow = guide.filter(ImageFilter.GaussianBlur(GEN_SIZE // 50))
        canvas = Image.composite(Image.new('RGB', canvas.size, accent), canvas, glow.point(lambda v: v // 3))
        if item is not None:
            canvas = Image.composite(Image.new('RGB', canvas.size, tuple(int(c * 0.8) for c in accent)), canvas, item)
        canvas = Image.composite(Image.new('RGB', canvas.size, accent), canvas, mask)
        features = has_inner_features(mask)
        parts = symbol_parts(mask) if multipart else []
        parts = parts if len(parts) >= MULTIPART_MIN_PARTS else None
        # Name the cut-outs (eyes, a keyhole) so the drawing keeps them dark.
        # Left free, the model kept styling symbols as glitch art; a subtle cue
        # from the theme's own motifs gives it a theme-fitting style instead.
        # (Its own random pick: the shared sequence drives layouts and
        # backgrounds, which must not change because of this cue.)
        motif = f", subtle {random.Random(seed + index * 7919 + 11).choice(motifs)} details" if motifs else ''
        if trait:
            # The theme's iconic item built into the symbol (the symbol stays the
            # main shape; the item is added to it, never a whole person).
            motif = (f", the symbol wearing big {trait} on top" if trait in ICONIC_WORN else
                     f", with a large {trait} crossed behind the symbol" if trait in ICONIC_HELD else
                     f", with {trait} added to the symbol")
        prompt = (f"{quality_tag + ', ' if sdxl else ''}ornate {finish} {subject} emblem, {accent_name} symbol with embossed "
                  f"details and bevel{', dark cut-out details' if features else ''}{motif}, decorative shape, single centered object, "
                  f"plain black background, {style_words} icon art")
        for attempt in range(first_attempt, first_attempt + tries):
            art = _symbol_art_attempt(mask, prompt, canvas, refs, index, attempt, features, parts, iconic=bool(trait),
                                      guide=guide)
            if art is not None:
                return art
        return None

    def _symbol_art_attempt(mask, prompt, canvas, refs, index, attempt, features, parts=None, iconic=False, guide=None):
        import numpy as np
        out = pipe(
            prompt=prompt,
            negative_prompt=('text, letters, people, face, scenery, landscape, background pattern, frame, border, blurry, low quality, '
                             'glitch, glitch effect, chromatic aberration, distortion, datamosh, pixel sorting, noise, broken shapes'),
            image=canvas, control_image=control_edges(guide if guide is not None else mask),
            strength=MULTIPART_ART_STRENGTH if parts else ICONIC_STRENGTH if iconic else SYMBOL_ART_STRENGTH,
            controlnet_conditioning_scale=MULTIPART_ART_GUIDE if parts else ICONIC_GUIDE if iconic else SYMBOL_ART_GUIDE,
            control_guidance_end=MULTIPART_ART_GUIDE_END if parts else ICONIC_GUIDE_END if iconic else SYMBOL_ART_GUIDE_END,
            ip_adapter_image=adapter_images(refs), num_inference_steps=SYMBOL_ART_STEPS, guidance_scale=7.0,
            generator=torch.Generator(device='cpu').manual_seed(seed + index * 7919 + 5 + attempt * 104729),
        ).images[0]
        rgb = np.asarray(out, dtype=np.float32)
        border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
        bg = np.median(border, axis=0)
        alpha = np.clip((np.sqrt(((rgb - bg) ** 2).sum(axis=2)) - 30) / 60, 0, 1)
        # Only what was drawn around the symbol's place counts.
        near = np.asarray(rank_filter(guide if guide is not None else mask, 2 * (GEN_SIZE // 16) + 1), dtype=np.float32) / 255
        alpha = alpha * near
        drawn = Image.fromarray((alpha * 255).astype(np.uint8), 'L').filter(ImageFilter.MedianFilter(3))
        shape_area = (np.asarray(mask) > 127).sum()
        drawn_area = (np.asarray(drawn) > 127).sum()
        ratio = drawn_area / shape_area if shape_area else 0.0
        # Judge the solid core only (the soft glow around a drawing is kept in
        # the art but isn't its shape), lined up with the guide shape first so
        # a slightly bigger or shifted drawing isn't penalised.
        core = drawn.point(lambda v: 255 if v > 160 else 0)
        overlap = symbol_overlap(fit_symbol(core, mask), mask) if core.getbbox() else 0.0
        # Designs with inner features (eyes and smiles, a keyhole, a slot) are
        # only recognisable if the drawing still shows them: a washed-out,
        # near-white drawing keeps the silhouette but loses the features.
        detail = feature_contrast(out, mask) if features else None
        # A symbol of several small pictures: each one must still be there (a
        # whole-shape match passed while one picture had become a splotch).
        weakest = None
        if parts:
            reach = 2 * max(2, GEN_SIZE // 96) + 1
            solid = np.asarray(core) > 0
            weakest = min(float((solid & (np.asarray(rank_filter(Image.fromarray((part * 255).astype(np.uint8), 'L'), reach)) > 0)).sum())
                          / max(1, part.sum()) for part in parts)
        # Iconic symbols may carry added features around them, but must still
        # draw most of the symbol itself (recognisable first).
        cover = None
        if iconic:
            own = np.asarray(mask) > 127
            cover = float((np.asarray(core) > 0)[own].mean()) if own.any() else 0.0
        area_max, shape_min = (ICONIC_AREA_MAX, 0.0) if iconic else (SYMBOL_ART_AREA[1], SYMBOL_ART_SHAPE)
        if (not (SYMBOL_ART_AREA[0] <= ratio <= area_max) or overlap < shape_min
                or (cover is not None and cover < ICONIC_COVER)
                or (detail is not None and detail < SYMBOL_ART_DETAIL)
                or (weakest is not None and weakest < MULTIPART_PART_COVER)):
            print(f'  symbol art rejected: drawn area {ratio:.2f}x the shape (allowed {SYMBOL_ART_AREA[0]}-{SYMBOL_ART_AREA[1]}), '
                  f'shape match {overlap:.2f} (needs {SYMBOL_ART_SHAPE})'
                  + ('' if detail is None else f', inner detail {detail:.0f} (needs {SYMBOL_ART_DETAIL})')
                  + ('' if weakest is None else f', weakest part {weakest:.2f} (needs {MULTIPART_PART_COVER})')
                  + ('' if cover is None else f', symbol covered {cover:.2f} (needs {ICONIC_COVER})'), flush=True)
            if os.environ.get('VTA_DEBUG_SYMBOLS'):
                out.save(os.path.join(job['output_dir'], f'_symbol_debug_{index}_{attempt}.png'))
            return None
        # A faintly drawn symbol came out see-through (ghosted): its body is made
        # fully opaque, only the soft glow around it stays soft.
        a = np.asarray(drawn, dtype=np.float32)
        body = a > 40
        if body.any():
            top = np.percentile(a[body], 90)
            if top < 250:
                a = np.clip(a * (250.0 / max(top, 1.0)), 0, 255)
        # The soft glow fringe around the drawing made symbols look blurred at
        # icon size: it is cut away, leaving a crisp edge (a short ramp, no halo).
        a = np.clip((a - SYMBOL_EDGE_CUT) * (255.0 / (255 - SYMBOL_EDGE_CUT - SYMBOL_EDGE_TOP)), 0, 255)
        drawn = Image.fromarray(a.astype(np.uint8), 'L')
        # Gaps in the design stay open: style and glow, but the background shows
        # through holes and between pieces as in the original icon.
        drawn = ImageChops.subtract(drawn, symbol_gaps(mask))
        if os.environ.get('VTA_DEBUG_SYMBOLS'):
            out.save(os.path.join(job['output_dir'], f'_symbol_debug_{index}_{attempt}_kept.png'))
            drawn.save(os.path.join(job['output_dir'], f'_symbol_debug_{index}_{attempt}_alpha.png'))
        return Image.merge('RGBA', (*out.split(), drawn))

    def redraw_character(char, rng):
        """The character redrawn in a new pose and expression, or None if the
        result doesn't clearly show the same single character."""
        import tempfile
        pose, expression = rng.choice(list(POSES)), rng.choice(EXPRESSIONS)
        pose_words = POSES[pose][0]
        ref = character_reference(char, GEN_SIZE)
        canny_net = pipe.controlnet
        if sdxl:
            # Swap the pose model onto the GPU only for this redraw.
            if not small_gpu:
                canny_net.to('cpu')
            openpose.to(device)
        pipe.controlnet = openpose
        pipe.set_ip_adapter_scale([0.0, 0.85])
        try:
            # The prompt asks for correct anatomy and the avoid list names the
            # usual failures; the identity check below is the backstop.
            out = pipe(
                prompt=(f"{quality_tag + ', ' if sdxl else ''}one {expression} anime character, {pose_words}, same face hair and outfit as the reference, "
                        "correct anatomy, natural hands, upper body, plain grey background, clean lineart"),
                negative_prompt=('bad anatomy, arm from head, limbs from head, extra arms, extra limbs, floating limbs, '
                                 'malformed hands, extra fingers, missing fingers, fused fingers, deformed, mutated, disfigured, '
                                 'distorted face, multiple people, text, watermark, blurry, cropped head' + (f', {negative_tag}' if sdxl else '')),
                image=ref, control_image=draw_pose(pose, GEN_SIZE), strength=0.9, controlnet_conditioning_scale=1.0,
                ip_adapter_image=[[neutral], [ref]], num_inference_steps=steps, guidance_scale=7.0,
                generator=torch.Generator(device='cpu').manual_seed(rng.randrange(1 << 30)),
            ).images[0]
        finally:
            pipe.controlnet = canny_net
            if sdxl:
                openpose.to('cpu')
                if not small_gpu:
                    canny_net.to(device)
            pipe.set_ip_adapter_scale([style_scale, 0.0])
        fd, tmp = tempfile.mkstemp(prefix='vta_pose_', suffix='.png')
        os.close(fd)
        try:
            out.save(tmp)
            found = find_characters(session, head_detector, [tmp], limit=3)
        finally:
            os.remove(tmp)
        if len(found) != 1 or char.get('head_crop') is None or found[0].get('head_crop') is None:
            return None
        if identity_diff(char['head_crop'], found[0]['head_crop']) > POSE_IDENTITY_DIFF:
            return None
        return dict(found[0], source=char.get('source'), pose=f'{expression}, {pose}')
    palette = palette_stats(wallpapers)
    colors = palette_colors(wallpapers)
    # The theme's own UI colours (theme.xml / Colors tab) pick the symbol's
    # accent so the icons match the theme's interface. They lead the whole
    # palette only when the art shares them (a red theme with red menus):
    # pulling everything toward them turned a blue and purple theme orange
    # because its bar was orange.
    # The user can override this in the dialog: 'auto' (decide from the art),
    # 'always' (they always lead) or 'never' (wallpaper colours only).
    color_mode = job.get('theme_color_mode', 'auto')
    theme_colors = [] if color_mode == 'never' else chromatic(parse_theme_colors(job.get('theme_colors')))
    agreement = theme_colors_in_art(theme_colors, wallpapers)
    print(f'theme colours in the art: {agreement:.0%} (mode: {color_mode})', flush=True)
    if theme_colors and (color_mode == 'always' or agreement >= THEME_COLOR_AGREEMENT):
        import numpy as np
        colors = theme_colors + [c for c in colors if c not in theme_colors]
        t_lab = _rgb_to_lab(np.array(theme_colors, dtype=np.float64)).mean(axis=0)
        t_mean, t_std = palette
        t_mean = t_mean.copy()
        t_mean[1:] = 0.5 * t_mean[1:] + 0.5 * t_lab[1:]  # pull the hue toward the theme's colours
        palette = (t_mean, t_std)
    background = tuple(int(sum(c[i] for c in colors[:3]) / min(3, len(colors))) for i in range(3))
    accent = symbol_color(theme_colors or colors, background)
    accent_name = color_name(accent)
    outline = tuple(int(c * 0.12) for c in accent)

    steps = int(job.get('steps', DEFAULT_STEPS))
    creativity = float(job.get('creativity', 0.75))
    consistency = float(job.get('consistency', 0.7))
    clarity = float(job.get('clarity', 0.5))
    seed = int(job.get('seed', 0))
    os.makedirs(job['output_dir'], exist_ok=True)

    designs = example_symbols(job.get('examples_dirs'), job['templates_dir'])
    icons = job['icons']
    # Deterministic per seed, varied per icon.
    rng = random.Random(seed)
    layouts = pick_layouts(style, icons, bool(characters), rng)
    # Best pictures first (visible face, size, sharpness, completeness), with a
    # little seeded jitter so generations still differ.
    order = sorted(range(len(characters)), key=lambda i: characters[i].get('quality', 0) + rng.uniform(0, 0.4), reverse=True)
    # Spread characters over the icons: each person (all their pictures count
    # as one) is used once while unused ones remain; a reuse gets a new framing.
    person = {i: min([i] + same_as.get(i, [])) for i in order}
    used_people, used_framings = {}, {}
    # Each picture is used in one icon only (the same character can return
    # from a different picture, or as a new AI pose when that's enabled).
    used_pictures = set()

    # Icons that end up without a character are redone as centred emblems:
    # keeping the character layout left the symbol small and off to one side
    # next to blank space.
    pending = list(enumerate(icons))
    as_emblem = set()
    while pending:
        index, icon_name = pending.pop(0)
        template_path = os.path.join(job['templates_dir'], icon_name)
        if not os.path.isfile(template_path):
            emit('skipped', name=icon_name, index=index, total=len(icons), reason='template not found')
            continue
        emit('progress', name=icon_name, index=index, total=len(icons))

        layout_name = 'emblem' if icon_name in as_emblem else layouts[index]
        layout = LAYOUTS[layout_name]
        # Example layouts must match the icon type: with or without a character.
        with_character = wants_character(icon_name, style, bool(characters)) and icon_name not in as_emblem
        options = [l for l in learned.get(icon_name, []) if bool(l['character']) == with_character]
        # Icons with typeset text (www, 31, PS4) stay full size: in a scene the
        # smaller symbol shrank the text past readable.
        if layout_name == 'emblem' and rng.random() < SCENE_SHARE and icon_name not in TEXT_REGIONS:
            layout, layout_name = LAYOUTS['scene'], 'scene'
        elif options and rng.random() < EXAMPLE_CHANCE.get(style, 0):
            layout, layout_name = rng.choice(options), 'example'
        layout = vary_layout(layout, rng, text=icon_name in TEXT_REGIONS)
        mask128 = symbol_mask(template_path, icon_name)
        template_mask128 = mask128
        # How a real theme author drew this same icon: its symbol guides the
        # design (recognisable by construction) and the icon joins the style refs.
        icon_refs = style_refs
        example_path = None
        if designs.get(icon_name) and rng.random() < EXAMPLE_SYMBOL_CHANCE:
            design, example_path = pick_design(designs[icon_name], template_mask128, rng)
            mask128 = fit_symbol(design, mask128)
            icon_refs = style_refs + [load_rgb(example_path, (GEN_SIZE, GEN_SIZE))]
        others = [path for _m, path in designs.get(icon_name, []) if path != example_path]
        if others:
            icon_refs = icon_refs + [load_rgb(path, (GEN_SIZE, GEN_SIZE))
                                     for path in rng.sample(others, min(EXAMPLE_STYLE_REFS, len(others)))]
        mask = place_symbol(mask128, layout, GEN_SIZE)
        cut, cut_sides, cut_reference = None, {}, None
        if order and layout['character']:
            # Never show a cut-off character: try this icon's character, then
            # other pictures of the same character, then the next characters.
            framings = LAYOUT_FRAMINGS.get(layout_name, LAYOUT_FRAMINGS['example'])
            picked = None
            rotated = order[index % len(order):] + order[:index % len(order)]
            # People not used yet first, then the least used; never past the cap.
            fresh = [i for i in rotated if used_people.get(person[i], 0) < MAX_USES_PER_PERSON]
            for who in sorted(fresh, key=lambda i: used_people.get(person[i], 0)):
                # The best copy of this character first (when it appears in several wallpapers).
                for candidate in sorted([who] + same_as.get(who, []), key=lambda i: characters[i].get('quality', 0), reverse=True):
                    if candidate in used_pictures and not pose_variation:
                        continue
                    picked = complete_framing(characters[candidate], framings, rng,
                                              avoid=used_framings.get(candidate, ()))
                    if picked:
                        break
                if picked:
                    break
            if not picked and redraw_on:
                # No uncut picture fits: use a cut one that the redraw will repair.
                for who in sorted(fresh, key=lambda i: used_people.get(person[i], 0)):
                    if who in used_pictures:
                        continue
                    candidate = who
                    picked = complete_framing(characters[who], framings, rng, allow_repair=True)
                    if picked:
                        break
            if picked and pose_variation and candidate in used_pictures:
                # A reuse: redraw the character in a new pose and expression.
                redrawn = redraw_character(characters[candidate], rng)
                fresh_pick = complete_framing(redrawn, framings, rng) if redrawn else None
                if fresh_pick:
                    picked = fresh_pick
                    print(f"{icon_name}: character redrawn ({redrawn['pose']})", flush=True)
                else:
                    # The same picture never appears twice: no character for this icon.
                    print(f'{icon_name}: pose redraw discarded (no clear match); icon left without a character', flush=True)
                    picked = None
            if picked:
                cut, cut_sides, framing = picked
                cut_reference = characters[candidate].get('head_crop')
                used_people[person[candidate]] = used_people.get(person[candidate], 0) + 1
                used_pictures.add(candidate)
                used_framings.setdefault(candidate, set()).add(framing)
                layout = anchor_cut_sides(layout, cut, cut_sides)
            else:
                # No usable picture: a full, centred emblem instead of a lopsided gap.
                layout, layout_name = vary_layout(LAYOUTS['emblem'], rng, text=icon_name in TEXT_REGIONS), 'emblem'
                mask = place_symbol(mask128, layout, GEN_SIZE)
        if cut and layout['front']:
            # Keep the symbol readable: if the character would hide much of
            # it, put the character behind the symbol instead.
            import numpy as np
            alpha = np.asarray(place_character(cut, layout, GEN_SIZE).getchannel('A')) > 127
            sym = np.asarray(mask) > 127
            if sym.any() and (alpha & sym).sum() / sym.sum() > MAX_SYMBOL_COVERED:
                layout = dict(layout, front=False)
        if cut and not layout['front'] and not (cut_sides.get('left') or cut_sides.get('right')):
            # A character behind the symbol must not have it over their face:
            # slide them sideways, away from the symbol, until the face is clear.
            # (Side-cut figures stay pinned to their icon edge.)
            layout = clear_face(layout, cut, mask, head_detector, mask128)
            mask = place_symbol(mask128, layout, GEN_SIZE)

        # Starting image: theme colours, the symbol in the accent colour, and
        # the real character in place, so the AI paints around them.
        char_layer = place_character(cut, layout, GEN_SIZE) if cut else None
        symbol_at = centre_of(mask) or (0.5, 0.5)
        char_at = centre_of(char_layer.getchannel('A')) if char_layer else None
        base = gradient_base(colors, index, GEN_SIZE, light=symbol_at, rim=char_at)
        # Symbol-only icons get one of two treatments (the layout, chosen by
        # the seed): a designed emblem (ornate medallion, radial symmetrical
        # design: the user's favourite look) or a scene, where the symbol sits
        # in a real, character-free piece of the theme's world.
        if char_layer:
            # Character icons get a real backdrop too, not a plain gradient.
            treatment = 'character-design' if rng.random() < CHARACTER_BACKDROP_DESIGN else 'character-scenery'
        else:
            # The designed emblem recipe (glow, light radiating from the symbol)
            # belongs to the big centred medallion of the user's favourite icon.
            # On a small or off-centre symbol that light spread across the whole
            # icon as random streaks and glows, so those compositions are set in
            # the theme's own world with calm light instead ('world').
            centred = layout.get('composition') in (None, 'medallion', 'nudged')
            treatment = 'scenery' if layout.get('scene') else 'design' if centred else 'world'
        # Every backdrop starts with some of the theme's real shapes to build
        # on (a plain gradient start stayed mostly plain); scenery treatments
        # get more of it.
        scenery = rng.choice(style_refs).filter(ImageFilter.GaussianBlur(2))
        environment = None
        if treatment == 'design':
            # The emblem is built on a real piece of the theme's art, redrawn as
            # its ornament (an emblem of effects alone had nothing of the theme).
            environment = clean_environment(rng)
        elif treatment in ('scenery', 'world'):
            # A real, seeded piece of the theme's world (or the user's extra
            # artwork), redrawn lightly so the place stays recognisable.
            environment = clean_environment(rng)
            if environment is None and treatment == 'scenery':
                # No clean piece of environment: the theme's softened scenery,
                # keeping the scene's placement (calm light, no emblem effects).
                print(f'{icon_name}: no character- and text-free environment found; using the theme\'s softened scenery', flush=True)
                treatment = 'world'
        if environment is not None:
            base = Image.blend(base, environment, DESIGN_ART_MIX if treatment == 'design' else SCENE_BASE_MIX)
            icon_refs = icon_refs + [environment]
        else:
            base = Image.blend(base, scenery, SCENERY_BASE_MIX if 'scenery' in treatment or treatment == 'world' else DESIGN_SCENERY_MIX)
        if char_layer and not layout['front']:
            base = Image.alpha_composite(base.convert('RGBA'), char_layer).convert('RGB')
        glow = mask.filter(ImageFilter.GaussianBlur(10))
        base = Image.composite(Image.new('RGB', base.size, accent), base, glow.point(lambda v: v // 2))
        base = Image.composite(Image.new('RGB', base.size, accent), base, mask)
        if char_layer and layout['front']:
            base = Image.alpha_composite(base.convert('RGBA'), char_layer).convert('RGB')
        guides = [mask] + ([char_layer.getchannel('A')] if char_layer else [])


        subject = ICON_SUBJECTS.get(icon_name, 'app symbol')
        # Tell the model where things are so the art is composed around them.
        where = f"symbol at the {placement_words(symbol_at)}"
        # The character's position is NOT described: words like "figure" made
        # the model paint invented bodies (the real character is placed later).
        # A different seed per icon varies the art; the palette match and the
        # shared style reference keep the set consistent.
        generator = torch.Generator(device='cpu').manual_seed(seed + index * 7919)
        # Art direction. The designed emblem uses, word for word, the recipe of
        # the user's favourite icon (FNF Tricky web emblem): its glow, radial
        # light, motifs and ornament ARE the look. Only random effects (stray
        # sparkles, flares, particles) are unwanted, and only outside that
        # recipe. No theme title: names in the prompt get drawn as text.
        icon_strength = creativity
        # The theme's own scenery and objects (seeded pick), named so symbol-only
        # backgrounds show them instead of only the theme's colours.
        picked = rng.sample(motifs, min(3, len(motifs))) if motifs else []
        motif_words = (', '.join(picked[:-1]) + ' and ' + picked[-1]) if len(picked) > 1 else (picked[0] if picked else '')
        if treatment == 'design':
            # The favourite sets the direction, not a fixed copy: each emblem
            # varies its ornament style and lighting (seeded).
            ornament, light = rng.choice(EMBLEM_ORNAMENTS), rng.choice(EMBLEM_LIGHTS)
            frame, finish = rng.choice(EMBLEM_FRAMES), rng.choice(EMBLEM_FINISHES)
            decoration = f"{motif_words} from the reference art" if motif_words else f"{ornament} and {subject} motifs"
            prompt = (f"{style_words} {subject} {finish} {frame} emblem icon, glowing {accent_name} symbol, {where}, decorated with "
                      f"{decoration}, {light}, ornate icon art, rich colors, strong contrast, colors of the reference art")
            icon_strength = DESIGN_ART_STRENGTH if environment is not None else DESIGN_SOFT_STRENGTH
            effects = ''
        elif treatment == 'scenery':
            prompt = (f"{style_words} {subject} emblem icon, glowing {accent_name} symbol, {where}, set in the same place as the "
                      f"reference art, its {motif_words or 'terrain and shapes'}, light radiating from the symbol, rich colors, colors of the reference art")
            icon_strength = SCENE_STRENGTH if environment is not None else SCENERY_STRENGTH
            effects = 'sparkles, lens flare, particles, '
        elif treatment == 'world':
            prompt = (f"{style_words} {subject} icon, clear {accent_name} symbol, {where}, set in the world of the "
                      f"reference art, its {motif_words or 'places, shapes and motifs'}, calm soft lighting, rich colors, colors of the reference art")
            icon_strength = SCENE_STRENGTH if environment is not None else SCENERY_STRENGTH
            effects = 'sparkles, lens flare, particles, light rays, light streaks, radial glow, '
        elif treatment == 'character-design':
            ornament, light = rng.choice(EMBLEM_ORNAMENTS), rng.choice(EMBLEM_LIGHTS)
            backdrop = motif_words or f"{ornament} and {subject} motifs"
            prompt = (f"{style_words} {subject} emblem icon, glowing {accent_name} symbol, {where}, detailed backdrop of {backdrop}, "
                      f"{light}, ornate icon art, rich colors, colors of the reference art")
            icon_strength = max(creativity, CHARACTER_BACKDROP_STRENGTH)
            effects = 'sparkles, lens flare, particles, '
        else:  # character-scenery
            prompt = (f"{style_words} {subject} emblem icon, glowing {accent_name} symbol, {where}, set in the same world as the "
                      f"reference art, soft rim light, ornate icon art, rich colors, colors of the reference art")
            icon_strength = SCENERY_STRENGTH
            effects = 'sparkles, lens flare, particles, '
        print(f"{icon_name}: {layout_name}/{layout.get('composition', '-')} layout, {treatment} background"
              f"{' on a clean scenery crop' if environment is not None else ''}, strength {icon_strength:.2f}", flush=True)
        if sdxl:
            prompt = f'{quality_tag}, {prompt}'
        # Characters come only from the real cut-outs and labels are typeset
        # afterwards, so people and text are never wanted. CLIP reads 77 tokens,
        # so this stays short (longer lists are silently cut off).
        negative = ('text, letters, words, watermark, logo, people, person, character, figure, silhouette, face, '
                    f'{effects}chaotic background, photo, blurry, low quality, deformed, cropped, border, frame')
        negative = f'{negative}, {style_avoid}'
        if sdxl:
            negative = f'{negative}, {negative_tag}'
        redraw_negative = ('text, letters, watermark, logo, extra people, extra limbs, extra arms, bad anatomy, deformed, '
                           'distorted face, cut off, cropped, blurry, low quality' + (f', {negative_tag}' if sdxl else ''))
        control = control_edges(*guides)
        # Steer this background's look toward the theme as much as its kind needs.
        icon_style = max(style_scale, STYLE_FOR.get(treatment, 0))
        pipe.set_ip_adapter_scale([icon_style, 0.0] if pose_variation else icon_style)
        real_figure = char_layer.getchannel('A') if char_layer else None
        result, fewest = None, None
        # Characters must come only from the theme's own art: if the model
        # paints people of its own, regenerate with a new seed and keep the
        # version with the fewest invented faces.
        for attempt in range(INVENTED_FIGURE_RETRIES + 1):
            if attempt:
                print(f'{icon_name}: regenerating (invented figures)', flush=True)
                generator = torch.Generator(device='cpu').manual_seed(seed + index * 7919 + attempt * 104729)
            attempt_img = pipe(
                prompt=prompt,
                negative_prompt=negative,
                image=base,
                control_image=control,
                strength=icon_strength,
                # The outline guide holds the symbol's shape for most of the
                # steps (so it stays recognisable), then lets go so the model
                # can style its surface and details.
                controlnet_conditioning_scale=0.75, control_guidance_end=0.85,
                ip_adapter_image=adapter_images(icon_refs),
                num_inference_steps=steps,
                guidance_scale=7.0,
                generator=generator,
            ).images[0]
            fakes = invented_figures(head_detector, attempt_img, real_figure, person_detector)
            if fewest is None or fakes < fewest:
                result, fewest = attempt_img, fakes
            if fakes == 0:
                break

        # Remove any lettering the model invented. A second inpainting pipeline
        # would slow every generation (measured 2.1s -> 5.2s per icon), so the
        # loaded pipeline lightly regenerates the icon and only the text spots
        # are copied back; whatever survives is blurred away.
        keep = mask if not char_layer else ImageChops.lighter(mask, char_layer.getchannel('A'))
        found = text_mask(text_detector, result, keep)
        # Repaint text spots (up to twice, new seed each time). Never blur them:
        # blurring left smudged patches on background objects with letter-like
        # detail; a rare leftover is better than a smudge.
        for attempt in range(TEXT_REPAINT_TRIES):
            if not has_text(found, TEXT_REPAINT_AREA):
                break
            print(f'{icon_name}: repainting invented text', flush=True)
            redo = pipe(
                prompt=prompt, negative_prompt=negative, image=result, control_image=control,
                strength=TEXT_REPAINT_STRENGTH, controlnet_conditioning_scale=0.75, control_guidance_end=0.85,
                ip_adapter_image=adapter_images(icon_refs), num_inference_steps=steps, guidance_scale=7.0,
                generator=torch.Generator(device='cpu').manual_seed(seed + index * 7919 + 1 + attempt * 31),
            ).images[0]
            result = Image.composite(redo, result, found.filter(ImageFilter.GaussianBlur(3)))
            found = text_mask(text_detector, result, keep)
        # Redraw the real character into the icon (or repair its cut), and
        # measure the result against the character with the identity model.
        drawn_in = False
        if char_layer is not None and redraw_on and cut_reference is not None:
            repair = bool(cut_sides.get('repair'))
            placed = Image.alpha_composite(result.convert('RGBA'), char_layer).convert('RGB')
            # A repair must not be held to the cut outline, so it only follows the symbol.
            redraw_control = control_edges(mask) if repair else control
            redo = pipe(
                prompt=f'{prompt}, clean edges, consistent lighting',
                negative_prompt=redraw_negative,
                image=placed, control_image=redraw_control,
                strength=REPAIR_STRENGTH if repair else REDRAW_STRENGTH,
                controlnet_conditioning_scale=0.75, control_guidance_end=0.85,
                ip_adapter_image=adapter_images(icon_refs), num_inference_steps=steps, guidance_scale=7.0,
                generator=torch.Generator(device='cpu').manual_seed(seed + index * 7919 + 2),
            ).images[0]
            # Keep the face exactly: blend the original face back in (feathered),
            # then measure the whole figure (hair, outfit, colours) against the
            # original with the identity model.
            redo = restore_face(head_detector, placed, redo, char_layer.getchannel('A'))
            match = figure_match(placed, redo, char_layer.getchannel('A'))
            accuracy.append(match)
            if match is not None and match <= REDRAW_MATCH:
                result, drawn_in = redo, True
                print(f"{icon_name}: character {'repaired and ' if repair else ''}redrawn into the icon "
                      f"(match {match:.3f}, limit {REDRAW_MATCH})", flush=True)
            elif repair:
                # Can't repair it faithfully and a cut must never show: redo this
                # icon from scratch as a centred emblem (painting the character
                # out left a lopsided gap).
                print(f'{icon_name}: cut character could not be repaired (match '
                      f"{'n/a' if match is None else f'{match:.3f}'}); redoing the icon as an emblem", flush=True)
                used_pictures.discard(candidate)
                used_people[person[candidate]] = max(0, used_people.get(person[candidate], 1) - 1)
                as_emblem.add(icon_name)
                pending.insert(0, (index, icon_name))
                continue
            else:
                print(f"{icon_name}: redraw didn't match the character closely enough "
                      f"(match {'n/a' if match is None else f'{match:.3f}'}); kept the original cut-out", flush=True)
        # Draw the symbol itself as artwork. A rejected drawing gets a second
        # chance guided by another author's design before the flat shape is used.
        multipart = icon_name in MULTIPART_ICONS
        # About half the symbols carry one of the theme's iconic features (own
        # random picks, so layouts and backgrounds are unaffected). Text icons
        # and many-part symbols stay plain: their readability comes first.
        trait_rng = random.Random(seed + index * 7919 + 19)
        trait = (trait_rng.choices(list(traits), weights=list(traits.values()))[0]
                 if traits and not multipart and icon_name not in TEXT_REGIONS and trait_rng.random() < ICONIC_SHARE else None)
        art_refs = icon_refs
        if trait and characters:
            # The theme's best character picture as a look reference for the feature.
            best = max(characters, key=lambda c: c.get('quality', 0))
            if best.get('head_crop') is not None:
                art_refs = icon_refs + [best['head_crop'].convert('RGB').resize((GEN_SIZE, GEN_SIZE))]
        art = draw_symbol_art(mask, subject, art_refs, rng, index, multipart=multipart, trait=trait)
        if trait and art is not None:
            print(f"{icon_name}: symbol drawn with the theme's {trait} feature", flush=True)
        if art is None and designs.get(icon_name):
            # Its own random pick: the shared sequence must stay the same for the
            # later icons' layouts and backgrounds whether or not a drawing was retried.
            design, alt_path = pick_design(designs[icon_name], template_mask128,
                                           random.Random(seed + index * 7919 + 13), exclude={example_path})
            if design is not None:
                alt128 = fit_symbol(design, template_mask128)
                alt_mask = place_symbol(alt128, layout, GEN_SIZE)
                art = draw_symbol_art(alt_mask, subject, icon_refs, rng, index, tries=1, first_attempt=SYMBOL_ART_TRIES,
                                      multipart=multipart)
                if art is not None:
                    mask128, mask = alt128, alt_mask
                    print(f"{icon_name}: symbol drawn from another community design", flush=True)
        if art is None:
            print(f"{icon_name}: drawn symbol didn't read as the app's symbol; using the design shape", flush=True)
        elif icon_name in TEXT_REGIONS:
            # Keep the text area clear: the label is typeset there afterwards
            # and nothing drawn (stray letters) may show through it.
            k = GEN_SIZE / ICON_SIZE
            x0, y0, x1, y1 = place_box(TEXT_REGIONS[icon_name][0], layout)
            pad = 3 * k
            clear = Image.new('L', art.size, 255)
            ImageDraw.Draw(clear).rectangle((x0 * k - pad, y0 * k - pad, x1 * k + pad, y1 * k + pad), fill=0)
            art.putalpha(ImageChops.multiply(art.getchannel('A'), clear))
        result = match_palette(result, palette, consistency)
        icon = result.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
        # Shrinking from GEN_SIZE softens fine detail: sharpen gently at icon size.
        icon = icon.filter(ImageFilter.UnsharpMask(radius=1, percent=60, threshold=2))
        art128 = None
        if art is not None:
            # Every symbol in the set shares one colour (the background-aware accent).
            art128 = recolor_symbol(art, accent).resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
            # Crisp at icon size: shrinking from GEN_SIZE softens the drawn symbol.
            art128 = Image.merge('RGBA', (*art128.convert('RGB').filter(ImageFilter.UnsharpMask(radius=1, percent=80, threshold=1)).split(),
                                          art128.getchannel('A')))
        symbol128 = place_symbol(mask128, layout, ICON_SIZE)
        char128 = outline_layer(place_character(cut, layout, ICON_SIZE), outline, 1) if cut and not drawn_in else None
        if drawn_in:
            # The character is already drawn into the icon: draw the symbol, then
            # restore the character over it if it stands in front.
            drawn = icon.copy()
            icon = apply_symbol(icon, symbol128, 1.0 if not layout['front'] else clarity, accent, art128)
            if layout['front']:
                over = place_character(cut, layout, ICON_SIZE).getchannel('A').filter(ImageFilter.MinFilter(3))
                icon = Image.composite(drawn, icon, over)
        elif char128 and not layout['front']:
            # Character behind: paste it, then the symbol fully on top.
            icon = Image.alpha_composite(icon.convert('RGBA'), char128).convert('RGB')
            icon = apply_symbol(icon, symbol128, 1.0, accent, art128)
        else:
            icon = apply_symbol(icon, symbol128, clarity, accent, art128)
            if char128:
                icon = Image.alpha_composite(icon.convert('RGBA'), char128).convert('RGB')
        if icon_name in TEXT_REGIONS:
            icon = stamp_text(icon, icon_name, accent, place_box(TEXT_REGIONS[icon_name][0], layout))
        out_path = os.path.join(job['output_dir'], icon_name)
        icon.save(out_path, 'PNG')
        emit('icon', name=icon_name, index=index, total=len(icons), path=out_path)

    if accuracy:
        measured = [a for a in accuracy if a is not None]
        kept = sum(1 for a in measured if a <= REDRAW_MATCH)
        summary = (f'character redraw accuracy: {kept}/{len(accuracy)} redraws matched the character'
                   + (f' (match scores {min(measured):.3f}-{max(measured):.3f}, limit {REDRAW_MATCH})' if measured else ''))
        print(summary, flush=True)
        emit('status', message=summary)
    if device == 'cuda':
        print(f'peak GPU memory: {torch.cuda.max_memory_reserved() / 1024 ** 3:.1f} GB ({model})', flush=True)
    emit_peak(torch.cuda.max_memory_reserved() if device == 'cuda' else 0, model)
    emit('done')


def main():
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    with open(sys.argv[1], 'r', encoding='utf-8') as f:
        job = json.load(f)
    try:
        run(job)
    except Exception as e:
        import traceback
        traceback.print_exc()
        emit('error', message=f'{type(e).__name__}: {e}')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
