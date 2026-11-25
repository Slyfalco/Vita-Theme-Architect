//! The Vita's animated home-screen background wave (theme.xml `m_waveType`),
//! drawn procedurally for the simulator. It shows through transparent parts of
//! the wallpapers. Each wave type is a palette — five colours from the top of
//! the screen to the bottom, the colour of the bright wave sheet, and where that
//! sheet sits — measured from screenshots of the console's waves 0-30; the
//! motion is drawn here (slow drifting light sheets), not copied.

use slint::{Image, Rgba8Pixel, SharedPixelBuffer};

/// Render size; the simulator scales it up (the wave is soft, so this is plenty).
pub const W: u32 = 192;
pub const H: u32 = 109;

/// Rows (fractions of the height) where the five palette colours sit.
const STOPS: [f32; 5] = [0.03, 0.25, 0.5, 0.72, 0.97];

/// (top..bottom colours, sheet colour, sheet height) per wave type 0-30.
const PALETTES: [([u32; 5], u32, f32); 31] = [
    ([0x0a1b85, 0x1937ad, 0x195bf8, 0x93d9ff, 0x57b8fe], 0xd6ffff, 0.83),
    ([0xb8c8e1, 0xa9b9e1, 0x92a2e1, 0xe3f3ff, 0xcad4f8], 0xf7feff, 0.75),
    ([0xff6049, 0xff4835, 0xff3a2a, 0xffc2c3, 0xfe5337], 0xffe0e5, 0.73),
    ([0xc82321, 0xb81828, 0xb70c33, 0xff4958, 0xd40f40], 0xff585e, 0.72),
    ([0x002e53, 0x005479, 0x07b0df, 0x3ee4ff, 0x15abe2], 0x4ff0ff, 0.78),
    ([0x410822, 0x4c0f26, 0x6b1433, 0x89143e, 0x54142e], 0x921445, 0.66),
    ([0x1a87fb, 0x3450f0, 0x3342f9, 0xc4e6ff, 0x54a6ff], 0xebfeff, 0.78),
    ([0xc9d7e5, 0x86acd4, 0x659dca, 0xafd9f5, 0xaec8de], 0xc6ebff, 0.75),
    ([0x030f26, 0x06183c, 0x072050, 0x072051, 0x071e4b], 0x0c3382, 0.90),
    ([0xde2543, 0xea425d, 0xfa9bae, 0xfff9fd, 0xf1789a], 0xfffeff, 0.74),
    ([0xdfb600, 0xd8ac02, 0xf3e94c, 0xffc12a, 0xf49f13], 0xfaf667, 0.57),
    ([0x000000, 0x000000, 0x000000, 0x000000, 0x000000], 0x000000, 0.50),
    ([0x1a2041, 0x31438f, 0xdaf0ff, 0x5973d1, 0x2a4093], 0xe4f8ff, 0.49),
    ([0x000000, 0x060927, 0x1d2d75, 0x152067, 0x06092b], 0x4776c5, 0.55),
    ([0xf7f2ce, 0xe6d560, 0xea9231, 0xfe7b46, 0xae1329], 0xf9f6dc, 0.70),
    ([0xfbc9b8, 0xf5a696, 0xf08478, 0xfef2f2, 0xfba892], 0xfff6f7, 0.75),
    ([0x178375, 0x248c69, 0xb4fee0, 0x6ec0a1, 0x32936a], 0xc7ffef, 0.52),
    ([0x0091c1, 0x019fce, 0x55ffff, 0x16daff, 0x05b1fc], 0x61ffff, 0.47),
    ([0x130a37, 0x2c0741, 0x4b025a, 0x7a029c, 0x420764], 0x8a12b1, 0.84),
    ([0xd3bfb8, 0xbaa39d, 0xb19494, 0xefcec7, 0xeac7c6], 0xfddacf, 0.82),
    ([0x3e2e21, 0x403427, 0x443a31, 0x704f42, 0x40362d], 0x815b4c, 0.73),
    ([0x3f0251, 0x7a16a6, 0xb12ee0, 0xff38fb, 0xd835f8], 0xff3fff, 0.76),
    ([0xd74766, 0xd52550, 0xff86aa, 0xff749b, 0xff1f40], 0xffc7c5, 0.57),
    ([0xdae0e7, 0xcbcfd6, 0xbabbc3, 0xe8ebf4, 0xc4c6d0], 0xf8fbff, 0.74),
    ([0xd39fd4, 0xc9a4e7, 0xedc4fe, 0xffe2ff, 0xcda8fe], 0xfff2ff, 0.64),
    ([0xa7d712, 0x67ac10, 0x81bd18, 0xa7e725, 0x52a812], 0xf7ff35, 0.65),
    ([0x01acd4, 0x14a1c8, 0x49c8f1, 0x7bdaff, 0x4cc4ef], 0x99efff, 0.77),
    ([0xdad904, 0xced20b, 0xdce24b, 0xf7faae, 0xd0db54], 0xffffd1, 0.74),
    ([0x3c4625, 0x4c5b3d, 0x7d8c75, 0xb4b6a4, 0x6a8067], 0xd1d4c7, 0.71),
    ([0x628e8e, 0x719b9e, 0xa3cac8, 0xd0edea, 0x6fbeb9], 0xeaffff, 0.74),
    ([0x515360, 0x4a5163, 0x7c8590, 0xbdc5c7, 0x82929d], 0xe1ebe7, 0.80),
];

/// A wave type's colours as floats.
#[derive(Clone, Copy)]
struct Palette {
    stops: [[f32; 3]; 5],
    glow: [f32; 3],
    glow_y: f32,
}

fn rgb(c: u32) -> [f32; 3] {
    [((c >> 16) & 255) as f32, ((c >> 8) & 255) as f32, (c & 255) as f32]
}

fn palette(kind: i32) -> Palette {
    let (stops, glow, glow_y) = PALETTES[kind.clamp(0, 30) as usize];
    Palette { stops: stops.map(rgb), glow: rgb(glow), glow_y }
}

fn lerp3(a: [f32; 3], b: [f32; 3], t: f32) -> [f32; 3] {
    [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t]
}

fn mix(a: Palette, b: Palette, t: f32) -> Palette {
    Palette {
        stops: std::array::from_fn(|i| lerp3(a.stops[i], b.stops[i], t)),
        glow: lerp3(a.glow, b.glow, t),
        glow_y: a.glow_y + (b.glow_y - a.glow_y) * t,
    }
}

/// One frame of the wave at `secs`, cross-faded from wave `from` to `to` by `fade` (0..1).
pub fn frame(from: i32, to: i32, fade: f32, secs: f32) -> Image {
    let p = mix(palette(from), palette(to), fade.clamp(0.0, 1.0));
    let mut buf = SharedPixelBuffer::<Rgba8Pixel>::new(W, H);
    let px = buf.make_mut_slice();
    // Three soft light sheets drifting at different speeds, like the console's.
    const SHEETS: [(f32, f32, f32, f32, f32, f32); 3] = [
        // (offset from glow_y, thickness, strength, x frequency, speed, phase)
        (0.0, 0.055, 0.85, 0.9, 0.21, 0.0),
        (-0.07, 0.09, 0.35, 0.6, -0.13, 1.7),
        (0.06, 0.12, 0.30, 1.3, 0.09, 4.1),
    ];
    let tau = std::f32::consts::TAU;
    for y in 0..H {
        let v = y as f32 / (H - 1) as f32;
        // Vertical gradient through the five palette stops.
        let seg = STOPS.iter().rposition(|s| v >= *s).unwrap_or(0).min(3);
        let span = STOPS[seg + 1] - STOPS[seg];
        let k = ((v - STOPS[seg]) / span).clamp(0.0, 1.0);
        let base = lerp3(p.stops[seg], p.stops[seg + 1], k * k * (3.0 - 2.0 * k));
        for x in 0..W {
            let u = x as f32 / (W - 1) as f32;
            let mut light = 0.0;
            for (off, thick, strength, freq, speed, phase) in SHEETS {
                // Sheets slant gently upward to the right, as on the console.
                let centre = p.glow_y + off - 0.05 * (u - 0.5)
                    + 0.045 * (tau * freq * u + secs * speed * tau * 0.25 + phase).sin()
                    + 0.02 * (tau * freq * 2.3 * u - secs * speed * tau * 0.4 + phase * 2.0).sin();
                let d = (v - centre) / thick;
                // Brighter where the sheet faces the viewer (across the middle).
                let face = 0.65 + 0.35 * (std::f32::consts::PI * u).sin();
                light += strength * face * (-d * d).exp();
            }
            let c = lerp3(base, p.glow, light.min(1.0));
            let i = (y * W + x) as usize;
            px[i] = Rgba8Pixel { r: c[0] as u8, g: c[1] as u8, b: c[2] as u8, a: 255 };
        }
    }
    Image::from_rgba8(buf)
}

#[cfg(test)]
mod tests {
    /// Writes a contact sheet of every wave type to $VTA_WAVE_SHEET (dev aid).
    #[test]
    #[ignore = "writes a preview image"]
    fn wave_sheet() {
        let Ok(out) = std::env::var("VTA_WAVE_SHEET") else { return };
        let (w, h) = (super::W, super::H);
        let mut sheet = image::RgbaImage::new(w * 4, h * 8);
        for k in 0..31 {
            let img = super::frame(k, k, 1.0, 3.0);
            let buf = img.to_rgba8().unwrap();
            for y in 0..h {
                for x in 0..w {
                    let p = buf.as_slice()[(y * w + x) as usize];
                    sheet.put_pixel((k as u32 % 4) * w + x, (k as u32 / 4) * h + y, image::Rgba([p.r, p.g, p.b, 255]));
                }
            }
        }
        sheet.save(out).unwrap();
    }
}