//! The Pillow operations v2 uses here: opening any image, `thumbnail`, the
//! upload encoding of `_image_part`, `convert('L')`, Lanczos resizes, the
//! 32-colour median-cut palette of the example sets, and `_looks_like_template`.
//! Sizes, modes and formats follow Pillow exactly; resampled pixels are close
//! but not bit-identical to Pillow's.

use super::py::PyError;
use base64::Engine;
use image::{DynamicImage, GenericImageView, RgbImage};
use std::path::Path;

/// `Image.open(...)` (+ load): decode any supported image.
pub fn open_bytes(bytes: &[u8]) -> Result<DynamicImage, PyError> {
    image::load_from_memory(bytes).map_err(|e| PyError::new("UnidentifiedImageError", format!("cannot identify image file: {e}")))
}

pub fn open_path(path: &Path) -> Result<DynamicImage, PyError> {
    let bytes = std::fs::read(path).map_err(|e| {
        let kind = if e.kind() == std::io::ErrorKind::NotFound { "FileNotFoundError" } else { "OSError" };
        PyError::new(kind, format!("{e}: '{}'", path.display()))
    })?;
    open_bytes(&bytes)
}

/// Whether Pillow would open these bytes in mode RGBA, LA or P (the modes
/// `_image_part` sends as PNG). PNG: by colour type; GIF: palette; others: by
/// decoded alpha.
pub fn pillow_alpha_or_palette(bytes: &[u8], decoded: &DynamicImage) -> bool {
    if bytes.len() > 25 && bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        return matches!(bytes[25], 3 | 4 | 6);
    }
    if bytes.starts_with(b"GIF8") {
        return true;
    }
    decoded.color().has_alpha()
}

/// The size `Image.thumbnail((max_w, max_h))` gives, or `None` when it leaves
/// the image as it is (Pillow's `preserve_aspect_ratio` rounding).
pub fn thumbnail_size(w: u32, h: u32, max_w: u32, max_h: u32) -> Option<(u32, u32)> {
    let (mut x, mut y) = (max_w as f64, max_h as f64);
    if x >= w as f64 && y >= h as f64 {
        return None;
    }
    let aspect = w as f64 / h as f64;
    // max(min(floor, ceil, key=key), 1): min keeps the first on a tie.
    let round = |number: f64, key: &dyn Fn(f64) -> f64| -> f64 {
        let (f, c) = (number.floor(), number.ceil());
        let pick = if key(c) < key(f) { c } else { f };
        pick.max(1.0)
    };
    if x / y >= aspect {
        let yy = y;
        x = round(y * aspect, &|n| (aspect - n / yy).abs());
    } else {
        let xx = x;
        y = round(x / aspect, &|n| if n == 0.0 { 0.0 } else { (aspect - xx / n).abs() });
    }
    let size = (x as u32, y as u32);
    (size != (w, h)).then_some(size)
}

/// `img.thumbnail((side, side))` (bicubic).
pub fn thumbnail(img: DynamicImage, side: u32) -> DynamicImage {
    match thumbnail_size(img.width(), img.height(), side, side) {
        Some((w, h)) => img.resize_exact(w, h, image::imageops::FilterType::CatmullRom),
        None => img,
    }
}

/// `img.resize(size, LANCZOS)` (a copy when the size is already right).
pub fn resize_lanczos(img: &RgbImage, w: u32, h: u32) -> RgbImage {
    if img.dimensions() == (w, h) {
        return img.clone();
    }
    image::imageops::resize(img, w, h, image::imageops::FilterType::Lanczos3)
}

/// v2 `_image_part(path, max_side)`: an inline image part for Gemini. PNG
/// (RGBA) for RGBA/LA/P images, so transparency survives; JPEG quality 90 (RGB)
/// otherwise.
pub fn image_part(path: &Path, max_side: u32) -> Result<serde_json::Value, PyError> {
    let bytes = std::fs::read(path).map_err(|e| {
        let kind = if e.kind() == std::io::ErrorKind::NotFound { "FileNotFoundError" } else { "OSError" };
        PyError::new(kind, format!("{e}: '{}'", path.display()))
    })?;
    let img = open_bytes(&bytes)?;
    let png = pillow_alpha_or_palette(&bytes, &img);
    let img = thumbnail(img, max_side);
    let mut buf = Vec::new();
    let mime = if png {
        img.to_rgba8()
            .write_to(&mut std::io::Cursor::new(&mut buf), image::ImageFormat::Png)
            .map_err(|e| PyError::new("OSError", e.to_string()))?;
        "image/png"
    } else {
        let rgb = img.to_rgb8();
        image::codecs::jpeg::JpegEncoder::new_with_quality(&mut buf, 90)
            .encode_image(&rgb)
            .map_err(|e| PyError::new("OSError", e.to_string()))?;
        "image/jpeg"
    };
    Ok(serde_json::json!({"inline_data": {"mime_type": mime, "data": base64::engine::general_purpose::STANDARD.encode(&buf)}}))
}

/// Save an RGB image as JPEG at `quality` (Pillow `save(..., quality=q)`).
pub fn save_jpeg(img: &RgbImage, path: &Path, quality: u8) -> Result<(), PyError> {
    let file = std::fs::File::create(path).map_err(|e| PyError::new("OSError", e.to_string()))?;
    image::codecs::jpeg::JpegEncoder::new_with_quality(std::io::BufWriter::new(file), quality)
        .encode_image(img)
        .map_err(|e| PyError::new("OSError", e.to_string()))
}

/// Pillow's `convert('L')` of one RGB pixel.
pub fn pil_luma(r: u8, g: u8, b: u8) -> u8 {
    ((r as u32 * 19595 + g as u32 * 38470 + b as u32 * 7471 + 0x8000) >> 16) as u8
}

/// Pillow's `convert('L')` of a whole image (greyscale sources keep their values).
pub fn to_pil_l(img: &DynamicImage) -> Vec<u8> {
    match img {
        DynamicImage::ImageLuma8(_) | DynamicImage::ImageLumaA8(_) => img.to_luma8().into_raw(),
        _ => img.to_rgb8().pixels().map(|p| pil_luma(p[0], p[1], p[2])).collect(),
    }
}

/// v2 `_looks_like_template`: true if a 128x128 icon is (nearly) the stock
/// template symbol (mean absolute greyscale difference under 12).
pub fn looks_like_template(img: &RgbImage, template_path: &Path) -> bool {
    if !template_path.is_file() {
        return false;
    }
    let Ok(t) = open_path(template_path) else { return false };
    let t = if t.dimensions() == (128, 128) {
        t
    } else {
        // L first, then the default (bicubic) resize, as v2 does.
        let l = image::GrayImage::from_raw(t.width(), t.height(), to_pil_l(&t)).expect("size matches");
        DynamicImage::ImageLuma8(image::imageops::resize(&l, 128, 128, image::imageops::FilterType::CatmullRom))
    };
    let b = to_pil_l(&t);
    let a: Vec<u8> = img.pixels().map(|p| pil_luma(p[0], p[1], p[2])).collect();
    if a.len() != b.len() || a.is_empty() {
        return false;
    }
    let total: f64 = a.iter().zip(&b).map(|(x, y)| (*x as f64 - *y as f64).abs()).sum();
    total / (a.len() as f64) < 12.0
}

/// `img.quantize(colors, method=MEDIANCUT, dither=NONE).save(path, optimize=True)`:
/// a median-cut palette (each pixel maps to its box's average colour) written
/// as an indexed PNG.
pub fn save_median_cut_png(img: &RgbImage, colors: usize, path: &Path) -> Result<(), PyError> {
    let (palette, indices) = median_cut(img, colors);
    let file = std::fs::File::create(path).map_err(|e| PyError::new("OSError", e.to_string()))?;
    let mut enc = png::Encoder::new(std::io::BufWriter::new(file), img.width(), img.height());
    enc.set_color(png::ColorType::Indexed);
    enc.set_depth(png::BitDepth::Eight);
    enc.set_compression(png::Compression::High);
    enc.set_palette(palette.iter().flat_map(|c| *c).collect::<Vec<u8>>());
    enc.write_header()
        .and_then(|mut w| w.write_image_data(&indices))
        .map_err(|e| PyError::new("OSError", e.to_string()))
}

/// Median cut over the colour histogram: split the box with the most pixels
/// along its widest channel at the pixel median, until `colors` boxes.
pub fn median_cut(img: &RgbImage, colors: usize) -> (Vec<[u8; 3]>, Vec<u8>) {
    let mut hist: std::collections::HashMap<[u8; 3], u32> = std::collections::HashMap::new();
    for p in img.pixels() {
        *hist.entry(p.0).or_default() += 1;
    }
    let mut entries: Vec<([u8; 3], u32)> = hist.into_iter().collect();
    entries.sort();
    let mut boxes: Vec<Vec<([u8; 3], u32)>> = vec![entries];
    let colors = colors.clamp(1, 256);
    while boxes.len() < colors {
        // The splittable box holding the most pixels.
        let Some(bi) = boxes
            .iter()
            .enumerate()
            .filter(|(_, b)| b.len() > 1)
            .max_by_key(|(i, b)| (b.iter().map(|e| e.1 as u64).sum::<u64>(), std::cmp::Reverse(*i)))
            .map(|(i, _)| i)
        else {
            break;
        };
        let mut b = std::mem::take(&mut boxes[bi]);
        let ch = (0..3)
            .max_by_key(|&c| {
                let lo = b.iter().map(|e| e.0[c]).min().unwrap_or(0);
                let hi = b.iter().map(|e| e.0[c]).max().unwrap_or(0);
                (hi - lo, std::cmp::Reverse(c))
            })
            .unwrap_or(0);
        b.sort_by_key(|e| (e.0[ch], e.0));
        let total: u64 = b.iter().map(|e| e.1 as u64).sum();
        let mut acc = 0u64;
        let mut cut = 1;
        for (i, e) in b.iter().enumerate() {
            acc += e.1 as u64;
            if acc * 2 >= total {
                cut = (i + 1).clamp(1, b.len() - 1);
                break;
            }
        }
        let right = b.split_off(cut);
        boxes[bi] = b;
        boxes.push(right);
    }
    let mut lookup = std::collections::HashMap::new();
    let palette: Vec<[u8; 3]> = boxes
        .iter()
        .enumerate()
        .map(|(i, b)| {
            let n: u64 = b.iter().map(|e| e.1 as u64).sum::<u64>().max(1);
            let mut sum = [0u64; 3];
            for (c, count) in b {
                lookup.insert(*c, i as u8);
                for k in 0..3 {
                    sum[k] += c[k] as u64 * *count as u64;
                }
            }
            [((sum[0] + n / 2) / n) as u8, ((sum[1] + n / 2) / n) as u8, ((sum[2] + n / 2) / n) as u8]
        })
        .collect();
    let indices = img.pixels().map(|p| lookup[&p.0]).collect();
    (palette, indices)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pillow_thumbnail_sizes() {
        // Values from Pillow 12 (Image.new(...).thumbnail(...)).
        assert_eq!(thumbnail_size(1920, 1080, 1024, 1024), Some((1024, 576)));
        assert_eq!(thumbnail_size(960, 544, 1024, 1024), None);
        assert_eq!(thumbnail_size(1000, 3000, 256, 256), Some((85, 256)));
        assert_eq!(thumbnail_size(333, 777, 256, 256), Some((110, 256)));
        assert_eq!(thumbnail_size(128, 128, 512, 512), None);
        assert_eq!(thumbnail_size(1001, 999, 256, 256), Some((256, 255)));
    }

    #[test]
    fn luma_matches_pillow() {
        assert_eq!(pil_luma(255, 0, 0), 76);
        assert_eq!(pil_luma(0, 255, 0), 150);
        assert_eq!(pil_luma(0, 0, 255), 29);
        assert_eq!(pil_luma(12, 200, 99), 132);
    }

    #[test]
    fn median_cut_limits_colours() {
        let img = RgbImage::from_fn(64, 64, |x, y| image::Rgb([(x * 4) as u8, (y * 4) as u8, ((x + y) * 2) as u8]));
        let (pal, idx) = median_cut(&img, 32);
        assert_eq!(pal.len(), 32);
        assert!(idx.iter().all(|i| (*i as usize) < 32));
        let flat = RgbImage::from_pixel(8, 8, image::Rgb([1, 2, 3]));
        let (pal, _) = median_cut(&flat, 32);
        assert_eq!(pal, vec![[1, 2, 3]]);
    }
}
