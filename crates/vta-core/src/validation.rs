//! Theme validation helpers (the creator's "Validation Engine" panel): WCAG
//! contrast for the accent/text legibility check. Asset-size and chrome counts
//! are computed in the app from the picked sources.

/// Parse an `AARRGGBB` (or `RRGGBB`) hex string to linear-ish sRGB 0..1 channels.
fn rgb(hex: &str) -> Option<(f64, f64, f64)> {
    let h = hex.trim_start_matches('#');
    // Use the last 6 digits (drop any alpha prefix).
    let rgb = if h.len() >= 6 { &h[h.len() - 6..] } else { return None };
    let v = u32::from_str_radix(rgb, 16).ok()?;
    let r = ((v >> 16) & 0xff) as f64 / 255.0;
    let g = ((v >> 8) & 0xff) as f64 / 255.0;
    let b = (v & 0xff) as f64 / 255.0;
    Some((r, g, b))
}

fn channel(c: f64) -> f64 {
    if c <= 0.03928 {
        c / 12.92
    } else {
        ((c + 0.055) / 1.055).powf(2.4)
    }
}

fn luminance(rgb: (f64, f64, f64)) -> f64 {
    0.2126 * channel(rgb.0) + 0.7152 * channel(rgb.1) + 0.0722 * channel(rgb.2)
}

/// WCAG 2.x contrast ratio between two colours (1.0–21.0). Returns 1.0 if either
/// colour can't be parsed.
pub fn contrast_ratio(fg: &str, bg: &str) -> f64 {
    let (Some(a), Some(b)) = (rgb(fg), rgb(bg)) else { return 1.0 };
    let (la, lb) = (luminance(a), luminance(b));
    let (hi, lo) = if la >= lb { (la, lb) } else { (lb, la) };
    (hi + 0.05) / (lo + 0.05)
}

/// WCAG compliance label for a contrast ratio (normal text thresholds).
pub fn contrast_level(ratio: f64) -> &'static str {
    if ratio >= 7.0 {
        "AAA"
    } else if ratio >= 4.5 {
        "AA"
    } else if ratio >= 3.0 {
        "AA Large"
    } else {
        "Low"
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn contrast_matches_wcag() {
        // White on black is the maximum, 21:1.
        assert!((contrast_ratio("ffffffff", "ff000000") - 21.0).abs() < 0.01);
        assert_eq!(contrast_level(contrast_ratio("ffffffff", "ff000000")), "AAA");
        // Same colour → 1:1.
        assert!((contrast_ratio("ff7bd0ff", "ff7bd0ff") - 1.0).abs() < 0.001);
        assert_eq!(contrast_level(1.0), "Low");
        // Alpha prefix is ignored; 6-digit also works.
        assert!((contrast_ratio("7bd0ff", "0d223a") - contrast_ratio("ff7bd0ff", "ff0d223a")).abs() < 0.001);
    }
}
