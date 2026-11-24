//! Building a theme folder from the creator's inputs: a port of v2's
//! `build_theme`, `validate_assets_on_build`, image processing (`process_and_save`)
//! and BGM conversion (`audio.convert_to_at9`).
//!
//! Images are resized to the Vita sizes and saved as 8-bit palette PNGs (with
//! per-entry transparency, as Pillow writes them). BGM is decoded with symphonia
//! (system ffmpeg as a fallback, like v2) and encoded with at9codec for the Vita
//! system-BGM size limit.

use crate::theme::{write_theme_xml, ThemeData, ICONS};
use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

/// Audio formats that can be encoded to AT9 (v2 `ENCODABLE_AUDIO_EXTS`).
pub const ENCODABLE_AUDIO_EXTS: [&str; 7] = [".wav", ".flac", ".ogg", ".opus", ".mp3", ".aiff", ".aif"];

/// Target sizes (width, height) for each asset id.
pub fn asset_size(id: &str) -> Option<(u32, u32)> {
    Some(match id {
        "preview_livearea.png" | "preview_lockscreen.png" => (480, 272),
        "preview_thumbnail.png" => (226, 128),
        "lockpaper.png" => (960, 512),
        "screen_other.png" | "screen_current.png" => (22, 22),
        "noNotice.png" | "newNotice.png" => (120, 110),
        id if id.starts_with("icon_") => (128, 128),
        id if id.starts_with("br") && id.ends_with(".png") => (960, 544),
        _ => return None,
    })
}

pub const THUMB_SIZE: (u32, u32) = (360, 192);

// ---------------------------------------------------------------- images

/// Load an image, remembering whether it was palette ("P") or greyscale ("L/LA")
/// as Pillow would see it.
enum Kind {
    Palette,
    Grey,
    Other,
}

fn source_kind(path: &Path) -> Kind {
    let Ok(file) = std::fs::File::open(path) else { return Kind::Other };
    let decoder = png::Decoder::new(std::io::BufReader::new(file));
    match decoder.read_info() {
        Ok(reader) => match reader.info().color_type {
            png::ColorType::Indexed => Kind::Palette,
            png::ColorType::Grayscale | png::ColorType::GrayscaleAlpha => Kind::Grey,
            _ => Kind::Other,
        },
        Err(_) => Kind::Other, // JPEG etc.
    }
}

/// v2 `process_and_save`: resize (Lanczos; nearest for palette sources, as Pillow
/// does) when the size differs, then save as an 8-bit PNG: palette (with alpha)
/// for colour images, greyscale kept greyscale.
pub fn convert_image(src: &Path, dest: &Path, size: (u32, u32)) -> Result<(), String> {
    let kind = source_kind(src);
    let img = image::open(src).map_err(|e| format!("Could not process image {}:\n{e}", src.display()))?;
    let resized = if (img.width(), img.height()) != size {
        let filter = match kind {
            Kind::Palette => image::imageops::FilterType::Nearest,
            _ => image::imageops::FilterType::Lanczos3,
        };
        img.resize_exact(size.0, size.1, filter)
    } else {
        img
    };
    let out = std::io::BufWriter::new(std::fs::File::create(dest).map_err(|e| e.to_string())?);
    match kind {
        Kind::Grey => {
            let has_alpha = resized.color().has_alpha();
            let mut enc = png::Encoder::new(out, size.0, size.1);
            enc.set_depth(png::BitDepth::Eight);
            if has_alpha {
                enc.set_color(png::ColorType::GrayscaleAlpha);
                let data = resized.to_luma_alpha8().into_raw();
                enc.write_header().and_then(|mut w| w.write_image_data(&data)).map_err(|e| e.to_string())
            } else {
                enc.set_color(png::ColorType::Grayscale);
                let data = resized.to_luma8().into_raw();
                enc.write_header().and_then(|mut w| w.write_image_data(&data)).map_err(|e| e.to_string())
            }
        }
        _ => write_palette_png(out, &resized.to_rgba8()),
    }
}

/// Quantise to ≤256 colours (exact when the image already has that few) and write
/// an indexed PNG with a tRNS chunk when any colour is translucent.
fn write_palette_png<W: std::io::Write>(out: W, rgba: &image::RgbaImage) -> Result<(), String> {
    let (w, h) = rgba.dimensions();
    let pixels = rgba.as_raw();
    // Exact palette if possible.
    let mut exact: Vec<[u8; 4]> = Vec::new();
    let mut lookup: std::collections::HashMap<[u8; 4], u8> = std::collections::HashMap::new();
    let mut indices: Vec<u8> = Vec::with_capacity((w * h) as usize);
    let mut fits = true;
    for px in pixels.chunks_exact(4) {
        let c = [px[0], px[1], px[2], px[3]];
        let idx = match lookup.get(&c) {
            Some(i) => *i,
            None => {
                if exact.len() == 256 {
                    fits = false;
                    break;
                }
                exact.push(c);
                let i = (exact.len() - 1) as u8;
                lookup.insert(c, i);
                i
            }
        };
        indices.push(idx);
    }
    let palette: Vec<[u8; 4]> = if fits {
        exact
    } else {
        let nq = color_quant::NeuQuant::new(10, 256, pixels);
        indices = pixels.chunks_exact(4).map(|p| nq.index_of(p) as u8).collect();
        // NeuQuant can drift alpha slightly below 255; an opaque image stays opaque.
        let opaque = pixels.chunks_exact(4).all(|p| p[3] == 255);
        nq.color_map_rgba()
            .chunks_exact(4)
            .map(|c| [c[0], c[1], c[2], if opaque { 255 } else { c[3] }])
            .collect()
    };
    let mut enc = png::Encoder::new(out, w, h);
    enc.set_color(png::ColorType::Indexed);
    enc.set_depth(png::BitDepth::Eight);
    enc.set_palette(palette.iter().flat_map(|c| [c[0], c[1], c[2]]).collect::<Vec<u8>>());
    if palette.iter().any(|c| c[3] != 255) {
        // tRNS may stop at the last non-opaque entry.
        let last = palette.iter().rposition(|c| c[3] != 255).unwrap_or(0);
        enc.set_trns(palette[..=last].iter().map(|c| c[3]).collect::<Vec<u8>>());
    }
    enc.write_header().and_then(|mut wr| wr.write_image_data(&indices)).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------- audio

/// Decode any supported audio file to interleaved 16-bit PCM (channels, rate, samples).
pub fn decode_audio(path: &Path) -> Result<(u16, u32, Vec<i16>), String> {
    decode_with_symphonia(path).or_else(|first| decode_with_ffmpeg(path).map_err(|_| first))
}

fn decode_with_symphonia(path: &Path) -> Result<(u16, u32, Vec<i16>), String> {
    use symphonia::core::codecs::audio::AudioDecoderOptions;
    use symphonia::core::formats::probe::Hint;
    use symphonia::core::formats::{FormatOptions, TrackType};
    use symphonia::core::io::MediaSourceStream;
    use symphonia::core::meta::MetadataOptions;

    let file = std::fs::File::open(path).map_err(|e| e.to_string())?;
    let mss = MediaSourceStream::new(Box::new(file), Default::default());
    let mut hint = Hint::new();
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }
    let mut format = symphonia::default::get_probe()
        .probe(&hint, mss, FormatOptions::default(), MetadataOptions::default())
        .map_err(|e| format!("Unsupported or unreadable audio: {e}"))?;
    let track = format.default_track(TrackType::Audio).ok_or("No audio track found.")?;
    let params = track.codec_params.as_ref().and_then(|p| p.audio()).ok_or("No audio track found.")?.clone();
    let track_id = track.id;
    let mut decoder = symphonia::default::get_codecs()
        .make_audio_decoder(&params, &AudioDecoderOptions::default())
        .map_err(|e| format!("Unsupported audio codec: {e}"))?;
    let (mut channels, mut rate) = (0u16, 0u32);
    let mut out: Vec<i16> = Vec::new();
    let mut chunk: Vec<i16> = Vec::new();
    loop {
        let packet = match format.next_packet() {
            Ok(Some(p)) => p,
            Ok(None) => break,
            Err(e) => return Err(format!("Could not read the audio: {e}")),
        };
        if packet.track_id != track_id {
            continue;
        }
        match decoder.decode(&packet) {
            Ok(buf) => {
                if channels == 0 {
                    channels = buf.spec().channels().count() as u16;
                    rate = buf.spec().rate();
                }
                chunk.resize(buf.samples_interleaved(), 0);
                buf.copy_to_slice_interleaved(&mut chunk);
                out.extend_from_slice(&chunk);
            }
            Err(symphonia::core::errors::Error::DecodeError(_)) => continue,
            Err(e) => return Err(format!("Could not decode the audio: {e}")),
        }
    }
    if channels == 0 || out.is_empty() {
        return Err("The audio file contains no samples.".into());
    }
    Ok((channels, rate, out))
}

/// v2 fallback: a system ffmpeg converts to 16-bit WAV (keeps rate and channels).
fn decode_with_ffmpeg(path: &Path) -> Result<(u16, u32, Vec<i16>), String> {
    let tmp = std::env::temp_dir().join(format!("vta_bgm_ffmpeg_{}.wav", std::process::id()));
    let status = std::process::Command::new("ffmpeg")
        .args(["-v", "error", "-y", "-i"])
        .arg(path)
        .args(["-c:a", "pcm_s16le"])
        .arg(&tmp)
        .stdin(std::process::Stdio::null())
        .status()
        .map_err(|e| e.to_string())?;
    if !status.success() {
        return Err("ffmpeg could not convert the audio".into());
    }
    let result = decode_with_symphonia(&tmp);
    let _ = std::fs::remove_file(&tmp);
    result
}

/// True for any non-.at9 BGM source (it gets encoded; the loop start applies).
pub fn bgm_needs_encoding(path: &str) -> bool {
    let p = path.trim().to_lowercase();
    !p.is_empty() && !p.ends_with(".at9")
}

/// Max theme-BGM size the Vita accepts (from the codec, never hard-coded).
pub fn vita_bgm_max_bytes() -> usize {
    at9codec::encode::SizeLimit::VitaSystemBgm.max_bytes()
}

/// v2 `convert_to_at9`: copy an .at9 (warning if over the Vita limit), else
/// decode and encode for the Vita system-BGM target. `loop_start`: None/0 = loop
/// the whole track, n > 0 = loop from sample n. Returns an optional warning.
pub fn convert_to_at9(src: &Path, dest: &Path, loop_start: Option<u32>, progress: &(dyn Fn(u32, u32) + Sync)) -> Result<Option<String>, String> {
    let is_at9 = src.extension().is_some_and(|e| e.eq_ignore_ascii_case("at9"));
    if is_at9 {
        let same = src.to_string_lossy().to_lowercase() == dest.to_string_lossy().to_lowercase();
        if !same {
            std::fs::copy(src, dest).map_err(|e| format!("Failed to copy BGM file: {e}"))?;
        }
        let size = std::fs::metadata(dest).map(|m| m.len() as usize).unwrap_or(0);
        let limit = vita_bgm_max_bytes();
        if limit > 0 && size > limit {
            return Ok(Some(format!(
                "This background music is larger than the PS Vita's AT9 size limit (about {:.0} MB) at {:.1} MB, so the Vita may not use it. Use a shorter track.",
                limit as f64 / (1024.0 * 1024.0),
                size as f64 / (1024.0 * 1024.0)
            )));
        }
        return Ok(None);
    }
    if !src.exists() {
        return Err(format!("Source file not found: {}", src.display()));
    }
    let (channels, rate, samples) = decode_audio(src)?;
    let opts = at9codec::encode::EncodeOptions {
        bitrate: 0,
        size_limit: at9codec::encode::SizeLimit::VitaSystemBgm,
        loop_start: Some(loop_start.filter(|n| *n > 0).unwrap_or(0)),
        loop_end: None,
        progress: Some(progress),
    };
    let out = at9codec::encode::encode_pcm16(&samples, channels, rate, &opts).map_err(|e| {
        if matches!(e, at9codec::Error::ExceedsSizeLimit) {
            "This background music is too long to fit the PS Vita's AT9 size limit even at the lowest bitrate. Use a shorter track.".to_string()
        } else {
            format!("Native AT9 encode failed: {e}")
        }
    })?;
    std::fs::write(dest, out).map_err(|e| e.to_string())?;
    Ok(None)
}

// ---------------------------------------------------------------- build

/// Everything the creator collects (source file paths, not yet in the theme folder).
#[derive(Debug, Clone, Default)]
pub struct BuildInput {
    pub title: String,
    pub author: String,
    pub version: String,
    /// Asset id -> source image path (absolute). Empty/missing = not set.
    pub sources: BTreeMap<String, String>,
    /// BGM source (any encodable format or .at9); empty = none.
    pub bgm: String,
    /// Loop start sample when the BGM is encoded.
    pub bgm_loop_start: Option<u32>,
    /// Colour id (`m_*`) -> AARRGGBB.
    pub colors: BTreeMap<String, String>,
    /// Page n (2..=10) -> wave type text.
    pub page_waves: BTreeMap<u32, String>,
    /// Page n (2..=10) -> AARRGGBB (used when not using one global font colour).
    pub page_colors: BTreeMap<u32, String>,
    pub use_global_font_color: bool,
}

impl BuildInput {
    fn src(&self, id: &str) -> Option<&str> {
        self.sources.get(id).map(String::as_str).filter(|s| !s.is_empty())
    }
}

/// v2 `validate_assets_on_build`. Returns the problems (empty = OK).
pub fn validate(input: &BuildInput) -> Vec<String> {
    let mut errors = Vec::new();
    let version_ok = {
        let b = input.version.as_bytes();
        b.len() == 5 && b[2] == b'.' && b.iter().enumerate().all(|(i, c)| i == 2 || c.is_ascii_digit())
    };
    if !version_ok {
        errors.push("Version must be in the '01.00' format.".to_string());
    }
    if input.title.is_empty() {
        errors.push("Theme Title cannot be empty.".into());
    }
    let mut check = |id: &str, path: Option<&str>, required: bool, types: Option<&[&str]>| -> bool {
        let Some(p) = path else {
            if required {
                errors.push(format!("{id} is required."));
            }
            return false;
        };
        if !Path::new(p).exists() {
            errors.push(format!("{id} file not found:\n{p}"));
            return false;
        }
        if let Some(types) = types {
            let low = p.to_lowercase();
            if !types.iter().any(|t| low.ends_with(t)) {
                errors.push(format!("{id} must be one of: {}", types.join(", ")));
                return false;
            }
        }
        true
    };
    for id in ["lockpaper.png", "br1.png", "preview_livearea.png", "preview_lockscreen.png", "preview_thumbnail.png"] {
        check(id, input.src(id), true, None);
    }
    let mut bgm_types: Vec<&str> = ENCODABLE_AUDIO_EXTS.to_vec();
    bgm_types.push(".at9");
    check("bgm.at9", Some(input.bgm.as_str()).filter(|s| !s.is_empty()), false, Some(&bgm_types));
    for (id, p) in &input.sources {
        if !["lockpaper.png", "br1.png", "preview_livearea.png", "preview_lockscreen.png", "preview_thumbnail.png"].contains(&id.as_str()) && !p.is_empty() {
            check(id, Some(p), false, None);
        }
    }
    errors
}

/// Progress of a build: (done steps, total steps, label).
pub type BuildProgress<'a> = &'a (dyn Fn(usize, usize, &str) + Sync);

/// v2 `build_theme`: write the theme folder (`dest`, created if needed) and its
/// theme.xml. Returns optional warnings (e.g. BGM issues, like v2's warning box).
pub fn build_theme(dest: &Path, input: &BuildInput, progress: BuildProgress) -> Result<Vec<String>, String> {
    std::fs::create_dir_all(dest).map_err(|e| e.to_string())?;
    let mut warnings = Vec::new();
    let mut data = ThemeData {
        title: Some(input.title.clone()),
        author: Some(input.author.clone()),
        version: Some(input.version.clone()),
        use_global_font_color: input.use_global_font_color,
        ..Default::default()
    };
    // Plan the steps so progress is meaningful.
    let mut steps: Vec<(String, PathBuf, String, (u32, u32))> = Vec::new(); // (asset id, src, dest name, size)
    for id in ["preview_livearea.png", "preview_lockscreen.png", "preview_thumbnail.png", "lockpaper.png"] {
        let src = input.src(id).ok_or_else(|| format!("{id} is required."))?;
        steps.push((id.into(), src.into(), id.into(), asset_size(id).unwrap()));
    }
    let br1 = input.src("br1.png").ok_or("br1.png is required.")?;
    steps.push(("br1.png".into(), br1.into(), "br1.png".into(), (960, 544)));
    steps.push(("tbr1.png".into(), br1.into(), "tbr1.png".into(), THUMB_SIZE));
    for i in 2..=10 {
        if let Some(src) = input.src(&format!("br{i}.png")) {
            steps.push((format!("br{i}.png"), src.into(), format!("br{i}.png"), (960, 544)));
            steps.push((format!("tbr{i}.png"), src.into(), format!("tbr{i}.png"), THUMB_SIZE));
        }
    }
    let mut others: Vec<&str> = ICONS.iter().map(|(_, id)| *id).collect();
    others.extend(["screen_other.png", "screen_current.png", "noNotice.png", "newNotice.png"]);
    for id in others {
        if let Some(src) = input.src(id) {
            steps.push((id.into(), src.into(), id.into(), asset_size(id).unwrap()));
        }
    }
    let total = steps.len() + usize::from(!input.bgm.is_empty()) + 1;
    let mut done = 0;

    // Convert every image into memory first: when editing, a source may be the
    // very file being replaced (e.g. br1.png feeding both br1.png and tbr1.png).
    let scratch = std::env::temp_dir().join(format!("vta_build_{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&scratch);
    std::fs::create_dir_all(&scratch).map_err(|e| e.to_string())?;
    let result = (|| -> Result<(), String> {
        for (id, src, name, size) in &steps {
            progress(done, total, &format!("Processing {name}…"));
            convert_image(src, &scratch.join(name), *size)?;
            data.assets.insert(id.clone(), name.clone());
            done += 1;
        }
        for (_, _, name, _) in &steps {
            std::fs::copy(scratch.join(name), dest.join(name)).map_err(|e| format!("Could not write {name}: {e}"))?;
        }
        Ok(())
    })();
    let _ = std::fs::remove_dir_all(&scratch);
    result?;

    data.page_waves.insert("br1_wave".into(), "12".into());
    let font = input.colors.get("m_fontColor").cloned().unwrap_or_else(|| "ffffffff".into());
    data.page_colors.insert("br1_color".into(), font);
    for i in 2..=10 {
        if input.src(&format!("br{i}.png")).is_some() {
            data.page_waves.insert(format!("br{i}_wave"), input.page_waves.get(&i).cloned().unwrap_or_else(|| "12".into()));
            if !input.use_global_font_color {
                if let Some(c) = input.page_colors.get(&i) {
                    data.page_colors.insert(format!("br{i}_color"), c.clone());
                }
            }
        }
    }

    if !input.bgm.is_empty() {
        progress(done, total, "Encoding background music…");
        let loop_start = if bgm_needs_encoding(&input.bgm) { input.bgm_loop_start } else { None };
        let bgm_dest = dest.join("bgm.at9");
        let at9_progress = |d: u32, t: u32| {
            if t > 0 {
                progress(done, total, &format!("Encoding background music… {}%", d * 100 / t));
            }
        };
        match convert_to_at9(Path::new(&input.bgm), &bgm_dest, loop_start, &at9_progress) {
            Ok(warning) => {
                // Editing a theme whose BGM is named e.g. BGM.at9 keeps that name (same file).
                let name = if Path::new(&input.bgm).parent() == Some(dest)
                    && Path::new(&input.bgm).extension().is_some_and(|e| e.eq_ignore_ascii_case("at9"))
                    && Path::new(&input.bgm).file_name().is_some_and(|n| n.to_string_lossy().eq_ignore_ascii_case("bgm.at9"))
                {
                    Path::new(&input.bgm).file_name().unwrap().to_string_lossy().into_owned()
                } else {
                    "bgm.at9".to_string()
                };
                data.assets.insert("bgm.at9".into(), name);
                warnings.extend(warning);
            }
            Err(e) => warnings.push(format!("Failed to convert/copy BGM file: {}\n{e}", input.bgm)),
        }
        done += 1;
    }

    data.colors = input.colors.clone();
    progress(done, total, "Writing theme.xml…");
    write_theme_xml(dest, &data).map_err(|e| format!("Could not write theme.xml: {e}"))?;
    progress(total, total, "Done");
    Ok(warnings)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn palette_png_keeps_alpha() {
        let dir = std::env::temp_dir().join(format!("vta-img-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mut img = image::RgbaImage::new(64, 64);
        for (x, y, p) in img.enumerate_pixels_mut() {
            *p = if (16..48).contains(&x) && (16..48).contains(&y) { image::Rgba([200, 50, 50, 255]) } else { image::Rgba([0, 0, 0, 0]) };
        }
        img.save(dir.join("src.png")).unwrap();
        convert_image(&dir.join("src.png"), &dir.join("out.png"), (128, 128)).unwrap();
        let dec = png::Decoder::new(std::io::BufReader::new(std::fs::File::open(dir.join("out.png")).unwrap()));
        let r = dec.read_info().unwrap();
        assert_eq!(r.info().color_type, png::ColorType::Indexed);
        assert_eq!((r.info().width, r.info().height), (128, 128));
        assert!(r.info().trns.is_some());
        let back = image::open(dir.join("out.png")).unwrap().to_rgba8();
        assert_eq!(back.get_pixel(0, 0)[3], 0);
        assert_eq!(back.get_pixel(64, 64)[3], 255);
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn many_colours_quantise_to_256() {
        let dir = std::env::temp_dir().join(format!("vta-img2-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let img = image::RgbImage::from_fn(300, 200, |x, y| image::Rgb([(x % 256) as u8, (y % 256) as u8, ((x + y) % 256) as u8]));
        img.save(dir.join("src.jpg")).unwrap();
        convert_image(&dir.join("src.jpg"), &dir.join("out.png"), (226, 128)).unwrap();
        let dec = png::Decoder::new(std::io::BufReader::new(std::fs::File::open(dir.join("out.png")).unwrap()));
        let r = dec.read_info().unwrap();
        assert_eq!(r.info().color_type, png::ColorType::Indexed);
        assert!(r.info().trns.is_none());
        std::fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn version_validation() {
        let mut i = BuildInput { title: "T".into(), version: "1.0".into(), ..Default::default() };
        assert!(validate(&i).iter().any(|e| e.contains("01.00")));
        i.version = "01.00".into();
        assert!(!validate(&i).iter().any(|e| e.contains("01.00")));
    }
}
