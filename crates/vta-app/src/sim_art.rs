//! Simulator-only artwork: theme icons drawn as the console draws its bubbles,
//! i.e. wrapped round a glossy sphere (squeezed toward the edges, lit from the
//! top left with a soft shine, shaded underneath, with a pale glassy rim).
//! Only the simulator's preview uses this; built themes keep the flat icons.

use slint::{Image, Rgba8Pixel, SharedPixelBuffer};

/// Output size (pixels) of a sphere bubble.
const SIZE: u32 = 192;

/// The icon wrapped round a lit sphere. Returns the input unchanged if its
/// pixels can't be read.
pub fn sphere(src: &Image) -> Image {
    let Some(buf) = src.to_rgba8() else { return src.clone() };
    let (sw, sh) = (buf.width() as f32, buf.height() as f32);
    if sw < 2.0 || sh < 2.0 {
        return src.clone();
    }
    let px = buf.as_slice();
    let sample = |x: f32, y: f32| -> [f32; 4] {
        // Bilinear sample, coordinates in source pixels.
        let x = x.clamp(0.0, sw - 1.001);
        let y = y.clamp(0.0, sh - 1.001);
        let (x0, y0) = (x.floor() as usize, y.floor() as usize);
        let (fx, fy) = (x - x0 as f32, y - y0 as f32);
        let w = buf.width() as usize;
        let at = |xx: usize, yy: usize| {
            let p = px[yy * w + xx];
            [p.r as f32, p.g as f32, p.b as f32, p.a as f32]
        };
        let (a, b, c, d) = (at(x0, y0), at(x0 + 1, y0), at(x0, y0 + 1), at(x0 + 1, y0 + 1));
        std::array::from_fn(|i| (a[i] * (1.0 - fx) + b[i] * fx) * (1.0 - fy) + (c[i] * (1.0 - fx) + d[i] * fx) * fy)
    };
    // Light from the top left, toward the viewer.
    let light = normalise([-0.45, -0.55, 0.70]);
    let mut out = SharedPixelBuffer::<Rgba8Pixel>::new(SIZE, SIZE);
    let o = out.make_mut_slice();
    let half = SIZE as f32 / 2.0;
    for y in 0..SIZE {
        for x in 0..SIZE {
            let u = (x as f32 + 0.5 - half) / half;
            let v = (y as f32 + 0.5 - half) / half;
            let r = (u * u + v * v).sqrt();
            let i = (y * SIZE + x) as usize;
            if r >= 1.0 {
                o[i] = Rgba8Pixel { r: 0, g: 0, b: 0, a: 0 };
                continue;
            }
            let z = (1.0 - r * r).sqrt();
            // Wrap: the picture is laid over the visible half of the sphere, so
            // it is squeezed toward the rim (equal-angle mapping).
            let k = if r > 1e-4 { (r.asin() / std::f32::consts::FRAC_PI_2) / r } else { 1.0 };
            let (su, sv) = (u * k * 0.98, v * k * 0.98);
            let c = sample((su * 0.5 + 0.5) * sw - 0.5, (sv * 0.5 + 0.5) * sh - 0.5);
            let alpha = c[3] / 255.0;
            // A transparent icon still reads as a glass ball.
            let base = [
                c[0] * alpha + 225.0 * (1.0 - alpha),
                c[1] * alpha + 232.0 * (1.0 - alpha),
                c[2] * alpha + 240.0 * (1.0 - alpha),
            ];
            let n = [u, v, z];
            let diffuse = dot(n, light).max(0.0);
            let shade = 0.62 + 0.48 * diffuse;
            // Soft shine: a broad highlight up and to the left.
            let h = normalise([light[0], light[1], light[2] + 1.0]);
            let spec = dot(n, h).max(0.0).powf(28.0) * 0.55 + dot(n, h).max(0.0).powf(6.0) * 0.10;
            // Pale glassy rim (fresnel), strongest at the edge.
            let rim = (1.0 - z).powf(2.2) * 0.55;
            let mut rgb = [0f32; 3];
            for ch in 0..3 {
                let lit = base[ch] * shade;
                let glass = lit + (215.0 - lit) * rim;
                rgb[ch] = (glass + 255.0 * spec).clamp(0.0, 255.0);
            }
            // Anti-aliased edge.
            let edge = ((1.0 - r) * half).clamp(0.0, 1.0);
            o[i] = Rgba8Pixel { r: rgb[0] as u8, g: rgb[1] as u8, b: rgb[2] as u8, a: (255.0 * edge) as u8 };
        }
    }
    Image::from_rgba8(out)
}

fn dot(a: [f32; 3], b: [f32; 3]) -> f32 {
    a[0] * b[0] + a[1] * b[1] + a[2] * b[2]
}

fn normalise(a: [f32; 3]) -> [f32; 3] {
    let l = dot(a, a).sqrt();
    [a[0] / l, a[1] / l, a[2] / l]
}

/// Frames of a bubble spinning in 3D (the launch animation): the icon on a
/// glossy lens (a flattened sphere) turning about an upright axis, so it
/// goes edge-on and swings round like the console's. `turn_deg` is the total
/// turn across all `frames`. A bubble without a theme icon spins as white glass.
pub fn spin_frames(src: &Image, frames: usize, turn_deg: f32) -> Vec<Image> {
    const N: u32 = 160;
    const THICK: f32 = 0.34; // lens half-thickness relative to its radius
    let buf = src.to_rgba8();
    let (sw, sh) = buf.as_ref().map(|b| (b.width() as f32, b.height() as f32)).unwrap_or((0.0, 0.0));
    let sample = |x: f32, y: f32| -> [f32; 4] {
        let Some(b) = buf.as_ref() else { return [236.0, 240.0, 246.0, 255.0] };
        if sw < 2.0 || sh < 2.0 {
            return [236.0, 240.0, 246.0, 255.0];
        }
        let x = ((x * 0.5 + 0.5) * sw - 0.5).clamp(0.0, sw - 1.001);
        let y = ((y * 0.5 + 0.5) * sh - 0.5).clamp(0.0, sh - 1.001);
        let (x0, y0) = (x.floor() as usize, y.floor() as usize);
        let (fx, fy) = (x - x0 as f32, y - y0 as f32);
        let w = b.width() as usize;
        let px = b.as_slice();
        let at = |xx: usize, yy: usize| {
            let p = px[yy * w + xx];
            [p.r as f32, p.g as f32, p.b as f32, p.a as f32]
        };
        let (a, bb, c, d) = (at(x0, y0), at(x0 + 1, y0), at(x0, y0 + 1), at(x0 + 1, y0 + 1));
        std::array::from_fn(|i| (a[i] * (1.0 - fx) + bb[i] * fx) * (1.0 - fy) + (c[i] * (1.0 - fx) + d[i] * fx) * fy)
    };
    // Upright axis: the console's launch spin does not tilt.
    let axis = [0.0, 1.0, 0.0];
    let light = normalise([-0.45, -0.55, 0.70]);
    let half = N as f32 / 2.0;
    (0..frames)
        .map(|f| {
            let ang = (f as f32 / (frames.max(2) - 1) as f32 * turn_deg).to_radians();
            // Rotation matrix about `axis` (Rodrigues); local = R^T * world.
            let (s, c) = ang.sin_cos();
            let (x, y, z) = (axis[0], axis[1], axis[2]);
            let r = [
                [c + x * x * (1.0 - c), x * y * (1.0 - c) - z * s, x * z * (1.0 - c) + y * s],
                [y * x * (1.0 - c) + z * s, c + y * y * (1.0 - c), y * z * (1.0 - c) - x * s],
                [z * x * (1.0 - c) - y * s, z * y * (1.0 - c) + x * s, c + z * z * (1.0 - c)],
            ];
            let to_local = |v: [f32; 3]| [
                r[0][0] * v[0] + r[1][0] * v[1] + r[2][0] * v[2],
                r[0][1] * v[0] + r[1][1] * v[1] + r[2][1] * v[2],
                r[0][2] * v[0] + r[1][2] * v[1] + r[2][2] * v[2],
            ];
            let to_world = |v: [f32; 3]| [
                r[0][0] * v[0] + r[0][1] * v[1] + r[0][2] * v[2],
                r[1][0] * v[0] + r[1][1] * v[1] + r[1][2] * v[2],
                r[2][0] * v[0] + r[2][1] * v[1] + r[2][2] * v[2],
            ];
            let mut out = SharedPixelBuffer::<Rgba8Pixel>::new(N, N);
            let o = out.make_mut_slice();
            let dir = to_local([0.0, 0.0, -1.0]);
            for py in 0..N {
                for px in 0..N {
                    let u = (px as f32 + 0.5 - half) / half;
                    let v = (py as f32 + 0.5 - half) / half;
                    let i = (py * N + px) as usize;
                    // Ray from the viewer (orthographic) into the lens, in lens
                    // space scaled so the lens is a unit sphere.
                    let p = to_local([u, v, 3.0]);
                    let (ps, ds) = ([p[0], p[1], p[2] / THICK], [dir[0], dir[1], dir[2] / THICK]);
                    let a = dot(ds, ds);
                    let b = 2.0 * dot(ps, ds);
                    let cc = dot(ps, ps) - 1.0;
                    let disc = b * b - 4.0 * a * cc;
                    if disc <= 0.0 {
                        o[i] = Rgba8Pixel { r: 0, g: 0, b: 0, a: 0 };
                        continue;
                    }
                    let tt = (-b - disc.sqrt()) / (2.0 * a);
                    let hit = [ps[0] + ds[0] * tt, ps[1] + ds[1] * tt, ps[2] + ds[2] * tt]; // unit sphere
                    // Normal of the lens (gradient of the scaled sphere), in world space.
                    let n_local = normalise([hit[0], hit[1], hit[2] / THICK]);
                    let n = to_world(n_local);
                    // The picture is on the front face; the back shows it mirrored.
                    let rr = (hit[0] * hit[0] + hit[1] * hit[1]).sqrt().min(1.0);
                    let k = if rr > 1e-4 { (rr.asin() / std::f32::consts::FRAC_PI_2) / rr } else { 1.0 };
                    let (tx, ty) = if hit[2] >= 0.0 { (hit[0] * k, hit[1] * k) } else { (-hit[0] * k, hit[1] * k) };
                    let col = sample(tx * 0.98, ty * 0.98);
                    let al = col[3] / 255.0;
                    let base = [col[0] * al + 225.0 * (1.0 - al), col[1] * al + 232.0 * (1.0 - al), col[2] * al + 240.0 * (1.0 - al)];
                    let diffuse = dot(n, light).max(0.0);
                    let shade = 0.55 + 0.55 * diffuse;
                    let h = normalise([light[0], light[1], light[2] + 1.0]);
                    let spec = dot(n, h).max(0.0).powf(28.0) * 0.6 + dot(n, h).max(0.0).powf(6.0) * 0.1;
                    let rim = (1.0 - n[2].abs()).powf(2.2) * 0.55;
                    let mut rgb = [0f32; 3];
                    for ch in 0..3 {
                        let lit = base[ch] * shade;
                        rgb[ch] = (lit + (215.0 - lit) * rim + 255.0 * spec).clamp(0.0, 255.0);
                    }
                    // Soft silhouette edge.
                    let edge = (disc.sqrt() * half * 0.5).clamp(0.0, 1.0);
                    o[i] = Rgba8Pixel { r: rgb[0] as u8, g: rgb[1] as u8, b: rgb[2] as u8, a: (255.0 * edge) as u8 };
                }
            }
            Image::from_rgba8(out)
        })
        .collect()
}
#[cfg(test)]
mod tests {
    /// Writes a sphere version of a template icon to $VTA_SPHERE_OUT (dev aid).
    #[test]
    #[ignore = "writes a preview image"]
    fn sphere_preview() {
        let Ok(out) = std::env::var("VTA_SPHERE_OUT") else { return };
        let bytes = include_bytes!("../../../Assets/IconTemplates/icon_calendar.png");
        let img = image::load_from_memory(bytes).unwrap().to_rgba8();
        let (w, h) = img.dimensions();
        let src = slint::Image::from_rgba8(slint::SharedPixelBuffer::clone_from_slice(img.as_raw(), w, h));
        let s = super::sphere(&src).to_rgba8().unwrap();
        let mut o = image::RgbaImage::new(s.width(), s.height());
        for (i, p) in s.as_slice().iter().enumerate() {
            o.put_pixel(i as u32 % s.width(), i as u32 / s.width(), image::Rgba([p.r, p.g, p.b, p.a]));
        }
        o.save(out).unwrap();
    }

    /// Writes a strip of the launch-spin frames to $VTA_SPIN_OUT (dev aid).
    #[test]
    #[ignore = "writes a preview image"]
    fn spin_preview() {
        let Ok(out) = std::env::var("VTA_SPIN_OUT") else { return };
        let bytes = include_bytes!("../../../Assets/IconTemplates/icon_calendar.png");
        let img = image::load_from_memory(bytes).unwrap().to_rgba8();
        let (w, h) = img.dimensions();
        let src = slint::Image::from_rgba8(slint::SharedPixelBuffer::clone_from_slice(img.as_raw(), w, h));
        let frames = super::spin_frames(&src, 12, 540.0);
        let n = 160;
        let mut o = image::RgbaImage::from_pixel(n * 6, n * 2, image::Rgba([40, 60, 120, 255]));
        for (k, f) in frames.iter().enumerate() {
            let b = f.to_rgba8().unwrap();
            for (i, p) in b.as_slice().iter().enumerate() {
                if p.a > 0 {
                    o.put_pixel((k as u32 % 6) * n + i as u32 % n, (k as u32 / 6) * n + i as u32 / n, image::Rgba([p.r, p.g, p.b, 255]));
                }
            }
        }
        o.save(out).unwrap();
    }
}