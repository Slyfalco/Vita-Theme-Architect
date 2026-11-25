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
}
