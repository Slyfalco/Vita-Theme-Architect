//! Native AT9 playback: at9codec decodes to PCM in memory and cpal plays it
//! (v3 drops VLC and the temp-WAV step). Looping is sample-accurate and uses the
//! loop points stored in the AT9 file.

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;

/// A decoded track.
pub struct Track {
    pub channels: u16,
    pub sample_rate: u32,
    pub samples: Vec<i16>,
    /// Loop region in frames (start, inclusive end), if the file defines one.
    pub loop_region: Option<(u64, u64)>,
    pub bitrate_kbps: u32,
}

impl Track {
    pub fn frames(&self) -> u64 {
        if self.channels == 0 {
            0
        } else {
            (self.samples.len() / self.channels as usize) as u64
        }
    }
    pub fn duration_secs(&self) -> f64 {
        self.frames() as f64 / self.sample_rate.max(1) as f64
    }
    /// Peak levels (0..1) in `bins` equal slices, for the waveform display.
    pub fn waveform(&self, bins: usize) -> Vec<f32> {
        let frames = self.frames() as usize;
        let ch = self.channels.max(1) as usize;
        if frames == 0 || bins == 0 {
            return vec![0.0; bins];
        }
        let mut out: Vec<f32> = (0..bins)
            .map(|b| {
                let (s, e) = (b * frames / bins, ((b + 1) * frames / bins).max(b * frames / bins + 1));
                let peak = self.samples[s * ch..(e * ch).min(self.samples.len())]
                    .iter()
                    .map(|v| (*v as i32).unsigned_abs())
                    .max()
                    .unwrap_or(0);
                peak as f32 / 32768.0
            })
            .collect();
        let max = out.iter().cloned().fold(0.0f32, f32::max).max(1e-6);
        for v in &mut out {
            *v = (*v / max).sqrt();
        }
        out
    }
    pub fn describe(&self) -> String {
        let ch = match self.channels {
            1 => "mono".to_string(),
            2 => "stereo".to_string(),
            n => format!("{n} ch"),
        };
        let looping = if self.loop_region.is_some() { " · loops" } else { "" };
        format!(
            "ATRAC9 · {} kHz · {ch} · {} kbps{looping}",
            trim_khz(self.sample_rate),
            self.bitrate_kbps
        )
    }
}

fn trim_khz(rate: u32) -> String {
    let khz = rate as f64 / 1000.0;
    if (khz - khz.round()).abs() < 1e-9 {
        format!("{}", khz.round())
    } else {
        format!("{khz:.1}")
    }
}

/// Decode an AT9 file.
pub fn decode_file(path: &Path) -> Result<Track, String> {
    let buf = std::fs::read(path).map_err(|e| format!("Could not read {}: {e}", path.display()))?;
    let container = at9codec::parse(&buf).map_err(|e| format!("Not a valid AT9 file: {e}"))?;
    let pcm = at9codec::decode_to_pcm16(&buf).map_err(|e| format!("Could not decode the AT9 audio: {e}"))?;
    let loop_region = container.loop_info.map(|l| (l.start as u64, l.end as u64));
    Ok(Track {
        channels: pcm.channels,
        sample_rate: pcm.sample_rate,
        samples: pcm.samples,
        loop_region,
        bitrate_kbps: container.avg_bytes_per_sec * 8 / 1000,
    })
}

/// Write a track as a 16-bit PCM WAV (v2 "Extract BGM" output).
pub fn write_wav(track: &Track, path: &Path) -> std::io::Result<()> {
    let file = std::io::BufWriter::new(std::fs::File::create(path)?);
    at9codec::wav::write_pcm16(file, track.channels, track.sample_rate, &track.samples)
}

/// Shared playback state between the UI thread and the audio callback.
struct Shared {
    track: Arc<Track>,
    /// Play position in source frames, as 32.32 fixed point (for resampling).
    pos: AtomicU64,
    playing: AtomicBool,
    looping: AtomicBool,
    finished: AtomicBool,
}

/// An open output stream playing one track.
pub struct Player {
    shared: Arc<Shared>,
    _stream: cpal::Stream,
}

const FRAC: u64 = 1 << 32;

impl Player {
    pub fn new(track: Arc<Track>, looping: bool) -> Result<Player, String> {
        let host = cpal::default_host();
        let device = host.default_output_device().ok_or("No audio output device was found.")?;
        let supported = device.default_output_config().map_err(|e| format!("Audio output error: {e}"))?;
        let config = supported.config();
        let shared = Arc::new(Shared {
            track,
            pos: AtomicU64::new(0),
            playing: AtomicBool::new(false),
            looping: AtomicBool::new(looping),
            finished: AtomicBool::new(false),
        });
        let stream = match supported.sample_format() {
            cpal::SampleFormat::F32 => build::<f32>(&device, config, shared.clone()),
            cpal::SampleFormat::I16 => build::<i16>(&device, config, shared.clone()),
            cpal::SampleFormat::U16 => build::<u16>(&device, config, shared.clone()),
            cpal::SampleFormat::I32 => build::<i32>(&device, config, shared.clone()),
            other => return Err(format!("Unsupported audio output format: {other:?}")),
        }?;
        stream.play().map_err(|e| format!("Audio output error: {e}"))?;
        Ok(Player { shared, _stream: stream })
    }

    pub fn play(&self) {
        if self.shared.finished.swap(false, Ordering::SeqCst) {
            self.shared.pos.store(0, Ordering::SeqCst);
        }
        self.shared.playing.store(true, Ordering::SeqCst);
    }
    pub fn pause(&self) {
        self.shared.playing.store(false, Ordering::SeqCst);
    }
    pub fn is_playing(&self) -> bool {
        self.shared.playing.load(Ordering::SeqCst)
    }
    pub fn set_looping(&self, on: bool) {
        self.shared.looping.store(on, Ordering::SeqCst);
    }
    /// Position 0..1.
    pub fn progress(&self) -> f32 {
        let frames = self.shared.track.frames().max(1);
        ((self.shared.pos.load(Ordering::SeqCst) / FRAC) as f64 / frames as f64).clamp(0.0, 1.0) as f32
    }
    pub fn position_secs(&self) -> f64 {
        (self.shared.pos.load(Ordering::SeqCst) / FRAC) as f64 / self.shared.track.sample_rate.max(1) as f64
    }
    pub fn seek(&self, fraction: f32) {
        let frame = (self.shared.track.frames() as f64 * fraction.clamp(0.0, 1.0) as f64) as u64;
        self.shared.finished.store(false, Ordering::SeqCst);
        self.shared.pos.store(frame * FRAC, Ordering::SeqCst);
    }
    pub fn track(&self) -> &Arc<Track> {
        &self.shared.track
    }
}

fn build<T>(device: &cpal::Device, config: cpal::StreamConfig, shared: Arc<Shared>) -> Result<cpal::Stream, String>
where
    T: cpal::SizedSample + cpal::FromSample<f32>,
{
    let out_channels = config.channels as usize;
    let step = ((shared.track.sample_rate as u128 * FRAC as u128) / config.sample_rate.max(1) as u128) as u64;
    let callback = move |data: &mut [T], _: &cpal::OutputCallbackInfo| {
        let track = &shared.track;
        let src_ch = track.channels.max(1) as usize;
        let frames = track.frames();
        let silence = T::from_sample(0.0f32);
        if !shared.playing.load(Ordering::Relaxed) || frames == 0 {
            data.iter_mut().for_each(|s| *s = silence);
            return;
        }
        let looping = shared.looping.load(Ordering::Relaxed);
        // Loop region: the file's own loop points, else the whole track.
        let (loop_start, loop_end) = track.loop_region.unwrap_or((0, frames - 1));
        let loop_end = loop_end.min(frames - 1);
        let mut pos = shared.pos.load(Ordering::Relaxed);
        for out in data.chunks_mut(out_channels) {
            let mut frame = pos / FRAC;
            if looping && frame > loop_end {
                pos = loop_start * FRAC + (pos - (loop_end + 1) * FRAC);
                frame = pos / FRAC;
            }
            if frame >= frames {
                shared.playing.store(false, Ordering::Relaxed);
                shared.finished.store(true, Ordering::Relaxed);
                pos = frames * FRAC;
                out.iter_mut().for_each(|s| *s = silence);
                continue;
            }
            // Linear interpolation between this frame and the next.
            let next = if looping && frame == loop_end { loop_start } else { (frame + 1).min(frames - 1) };
            let t = (pos % FRAC) as f32 / FRAC as f32;
            for (c, sample) in out.iter_mut().enumerate() {
                let sc = if src_ch == 1 { 0 } else { c };
                let v = if sc < src_ch {
                    let a = track.samples[frame as usize * src_ch + sc] as f32;
                    let b = track.samples[next as usize * src_ch + sc] as f32;
                    (a + (b - a) * t) / 32768.0
                } else {
                    0.0
                };
                *sample = T::from_sample(v);
            }
            pos += step;
        }
        shared.pos.store(pos, Ordering::Relaxed);
    };
    device
        .build_output_stream::<T, _, _>(config, callback, |e| eprintln!("audio stream error: {e}"), None)
        .map_err(|e| format!("Audio output error: {e}"))
}

pub fn format_time(secs: f64) -> String {
    let s = secs.max(0.0).round() as u64;
    format!("{:02}:{:02}", s / 60, s % 60)
}
