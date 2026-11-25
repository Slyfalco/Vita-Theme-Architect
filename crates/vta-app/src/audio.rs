//! Native AT9 playback by **streaming**: at9codec's `StreamingDecoder` decodes
//! ATRAC9 superframes on demand (like an Ogg packet decoder) on a background
//! thread that feeds a ring buffer, and the cpal callback drains it. The whole
//! PCM is never held in memory and no temporary WAV is written. Looping is
//! sample-accurate using the file's own loop points (the decoder seeks back to
//! the loop start, which is bit-exact).

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::collections::VecDeque;
use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::JoinHandle;

/// Waveform peak resolution computed at decode time (downsampled for the UI).
const WAVEFORM_RES: usize = 256;

/// One source of interleaved 16-bit PCM: a streaming AT9 decoder, or an in-memory
/// buffer (the bundled chime). Both decode/seek on demand.
enum Source {
    At9(Box<at9codec::StreamingDecoder>),
    Mem { samples: Arc<Vec<i16>>, channels: u16, pos: usize },
}

impl Source {
    fn read(&mut self, out: &mut [i16]) -> usize {
        match self {
            Source::At9(d) => d.read(out),
            Source::Mem { samples, pos, .. } => {
                let n = out.len().min(samples.len() - *pos);
                out[..n].copy_from_slice(&samples[*pos..*pos + n]);
                *pos += n;
                n
            }
        }
    }
    fn seek(&mut self, frame: u64) {
        match self {
            Source::At9(d) => d.seek(frame),
            Source::Mem { samples, channels, pos, .. } => {
                *pos = (frame as usize * (*channels).max(1) as usize).min(samples.len());
            }
        }
    }
    fn channels(&self) -> u16 {
        match self {
            Source::At9(d) => d.channels(),
            Source::Mem { channels, .. } => *channels,
        }
    }
}

/// A playable track: metadata + the compressed source, from which a [`Player`]
/// builds a fresh streaming decoder. No decoded PCM is retained.
pub struct Track {
    pub channels: u16,
    pub sample_rate: u32,
    pub total_frames: u64,
    /// Loop region in frames (start, inclusive end), if the file defines one.
    pub loop_region: Option<(u64, u64)>,
    pub bitrate_kbps: u32,
    kind: TrackSource,
}

#[derive(Clone)]
enum TrackSource {
    /// The complete AT9 file bytes (decoded on demand during playback).
    At9(Arc<Vec<u8>>),
    /// Already-decoded PCM (the chime).
    Pcm(Arc<Vec<i16>>),
}

impl Track {
    pub fn duration_secs(&self) -> f64 {
        self.total_frames as f64 / self.sample_rate.max(1) as f64
    }
    pub fn describe(&self) -> String {
        let ch = match self.channels {
            1 => "mono".to_string(),
            2 => "stereo".to_string(),
            n => format!("{n} ch"),
        };
        let looping = if self.loop_region.is_some() { " · loops" } else { "" };
        format!("ATRAC9 · {} kHz · {ch} · {} kbps{looping}", trim_khz(self.sample_rate), self.bitrate_kbps)
    }

    fn make_source(&self) -> Result<Source, String> {
        match &self.kind {
            TrackSource::At9(bytes) => {
                let d = at9codec::StreamingDecoder::new(bytes).map_err(|e| format!("Could not decode the AT9 audio: {e}"))?;
                Ok(Source::At9(Box::new(d)))
            }
            TrackSource::Pcm(samples) => Ok(Source::Mem { samples: samples.clone(), channels: self.channels, pos: 0 }),
        }
    }
}

/// Max-pool a peak array to `bins` buckets for the waveform display.
pub fn downsample(peaks: &[f32], bins: usize) -> Vec<f32> {
    if bins == 0 || peaks.is_empty() {
        return vec![0.0; bins];
    }
    (0..bins)
        .map(|b| {
            let (s, e) = (b * peaks.len() / bins, ((b + 1) * peaks.len() / bins).max(b * peaks.len() / bins + 1));
            peaks[s..e.min(peaks.len())].iter().cloned().fold(0.0f32, f32::max)
        })
        .collect()
}

fn trim_khz(rate: u32) -> String {
    let khz = rate as f64 / 1000.0;
    if (khz - khz.round()).abs() < 1e-9 {
        format!("{}", khz.round())
    } else {
        format!("{khz:.1}")
    }
}

/// Read an AT9 file's metadata only (from the header; no audio is decoded), so
/// playback can start streaming immediately. The waveform is computed separately
/// via [`Track::compute_waveform`].
pub fn decode_file(path: &Path) -> Result<Track, String> {
    let buf = std::fs::read(path).map_err(|e| format!("Could not read {}: {e}", path.display()))?;
    let container = at9codec::parse(&buf).map_err(|e| format!("Not a valid AT9 file: {e}"))?;
    let loop_region = container.loop_info.as_ref().and_then(|l| if l.is_whole { None } else { Some((l.start as u64, l.end as u64)) });
    Ok(Track {
        channels: container.channels,
        sample_rate: container.sample_rate,
        total_frames: container.total_samples as u64,
        loop_region,
        bitrate_kbps: container.avg_bytes_per_sec * 8 / 1000,

        kind: TrackSource::At9(Arc::new(buf)),
    })
}

impl Track {
    /// Compute waveform peaks with a one-time streaming decode pass (peaks only,
    /// PCM discarded). Meant to run on a background thread; returns `WAVEFORM_RES`
    /// normalised peaks. AT9 only (the chime has no waveform).
    pub fn compute_waveform(&self) -> Vec<f32> {
        let TrackSource::At9(bytes) = &self.kind else { return Vec::new() };
        let Ok(mut sd) = at9codec::StreamingDecoder::new(bytes) else { return Vec::new() };
        let ch = self.channels.max(1) as usize;
        let total = self.total_frames.max(1);
        let mut peaks = vec![0f32; WAVEFORM_RES];
        let mut frame = 0u64;
        let mut chunk = vec![0i16; 8192 * ch];
        loop {
            let n = sd.read(&mut chunk);
            if n == 0 {
                break;
            }
            for f in chunk[..n].chunks_exact(ch) {
                let bin = ((frame * WAVEFORM_RES as u64 / total) as usize).min(WAVEFORM_RES - 1);
                let peak = f.iter().map(|v| (*v as i32).unsigned_abs()).max().unwrap_or(0) as f32 / 32768.0;
                if peak > peaks[bin] {
                    peaks[bin] = peak;
                }
                frame += 1;
            }
        }
        let max = peaks.iter().cloned().fold(0.0f32, f32::max).max(1e-6);
        for v in &mut peaks {
            *v = (*v / max).sqrt();
        }
        peaks
    }
}

/// Decode an AT9 file straight to a 16-bit PCM WAV (the "Extract BGM" export).
/// This path legitimately needs the whole signal, so it decodes in full.
pub fn decode_to_wav(src: &Path, dest: &Path) -> Result<(), String> {
    let buf = std::fs::read(src).map_err(|e| format!("Could not read {}: {e}", src.display()))?;
    let pcm = at9codec::decode_to_pcm16(&buf).map_err(|e| format!("Could not decode the AT9 audio: {e}"))?;
    let file = std::io::BufWriter::new(std::fs::File::create(dest).map_err(|e| e.to_string())?);
    at9codec::wav::write_pcm16(file, pcm.channels, pcm.sample_rate, &pcm.samples).map_err(|e| e.to_string())
}

// ---------------------------------------------------------------- player

/// State shared between the UI, the decode thread and the audio callback.
struct Shared {
    /// Interleaved, device-rate, device-channel f32 samples waiting to be played.
    ring: Mutex<VecDeque<f32>>,
    /// Wakes the decode thread when the ring drains or a control changes.
    wake: Condvar,
    playing: AtomicBool,
    stop: AtomicBool,
    finished: AtomicBool,
    looping: AtomicBool,
    seek_pending: AtomicBool,
    seek_frame: AtomicU64,
    /// Source frame the decoder has produced up to (ahead of playback by the ring).
    decoded_pos: AtomicU64,
    total_frames: u64,
    src_rate: u32,
    dev_rate: u32,
    dev_channels: u16,
}

/// An open output stream streaming one track.
pub struct Player {
    shared: Arc<Shared>,
    _stream: cpal::Stream,
    thread: Option<JoinHandle<()>>,
}

impl Drop for Player {
    fn drop(&mut self) {
        self.shared.stop.store(true, Ordering::SeqCst);
        self.shared.wake.notify_all();
        if let Some(t) = self.thread.take() {
            let _ = t.join();
        }
    }
}

impl Player {
    pub fn new(track: Arc<Track>, looping: bool) -> Result<Player, String> {
        let host = cpal::default_host();
        let device = host.default_output_device().ok_or("No audio output device was found.")?;
        let supported = device.default_output_config().map_err(|e| format!("Audio output error: {e}"))?;
        let config = supported.config();
        let shared = Arc::new(Shared {
            ring: Mutex::new(VecDeque::new()),
            wake: Condvar::new(),
            playing: AtomicBool::new(false),
            stop: AtomicBool::new(false),
            finished: AtomicBool::new(false),
            looping: AtomicBool::new(looping),
            seek_pending: AtomicBool::new(false),
            seek_frame: AtomicU64::new(0),
            decoded_pos: AtomicU64::new(0),
            total_frames: track.total_frames,
            src_rate: track.sample_rate.max(1),
            dev_rate: config.sample_rate.max(1),
            dev_channels: config.channels,
        });
        let source = track.make_source()?;
        let stream = match supported.sample_format() {
            cpal::SampleFormat::F32 => build::<f32>(&device, config.clone(), shared.clone()),
            cpal::SampleFormat::I16 => build::<i16>(&device, config.clone(), shared.clone()),
            cpal::SampleFormat::U16 => build::<u16>(&device, config.clone(), shared.clone()),
            cpal::SampleFormat::I32 => build::<i32>(&device, config.clone(), shared.clone()),
            other => return Err(format!("Unsupported audio output format: {other:?}")),
        }?;
        stream.play().map_err(|e| format!("Audio output error: {e}"))?;
        let thread = {
            let shared = shared.clone();
            std::thread::spawn(move || decode_loop(shared, source))
        };
        Ok(Player { shared, _stream: stream, thread: Some(thread) })
    }

    pub fn play(&self) {
        if self.shared.finished.swap(false, Ordering::SeqCst) {
            self.request_seek_frame(0);
        }
        self.shared.playing.store(true, Ordering::SeqCst);
        self.shared.wake.notify_all();
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
    /// Playback position (source frames), i.e. decoded minus what's still buffered.
    fn play_pos(&self) -> u64 {
        let buffered = {
            let ring = self.shared.ring.lock().unwrap();
            ring.len() as u64 / self.shared.dev_channels.max(1) as u64
        };
        let buffered_src = buffered * self.shared.src_rate as u64 / self.shared.dev_rate as u64;
        self.shared.decoded_pos.load(Ordering::SeqCst).saturating_sub(buffered_src)
    }
    pub fn progress(&self) -> f32 {
        (self.play_pos() as f64 / self.shared.total_frames.max(1) as f64).clamp(0.0, 1.0) as f32
    }
    pub fn position_secs(&self) -> f64 {
        self.play_pos() as f64 / self.shared.src_rate as f64
    }
    pub fn seek(&self, fraction: f32) {
        let frame = (self.shared.total_frames as f64 * fraction.clamp(0.0, 1.0) as f64) as u64;
        self.request_seek_frame(frame);
    }
    fn request_seek_frame(&self, frame: u64) {
        self.shared.finished.store(false, Ordering::SeqCst);
        self.shared.seek_frame.store(frame, Ordering::SeqCst);
        self.shared.seek_pending.store(true, Ordering::SeqCst);
        self.shared.wake.notify_all();
    }
}

/// The decode thread: pull PCM from the source, resample to the device rate, and
/// keep the ring buffer topped up. Loops by seeking to the loop start.
fn decode_loop(shared: Arc<Shared>, mut source: Source) {
    let src_ch = source.channels().max(1) as usize;
    let dev_ch = shared.dev_channels.max(1) as usize;
    let step = shared.src_rate as f64 / shared.dev_rate as f64; // source frames per device frame
    // Target ~200 ms of buffered device audio.
    let target = (shared.dev_rate as usize / 5) * dev_ch;
    let mut resampler = Resampler::new(src_ch);
    let mut scratch = vec![0i16; 4096 * src_ch];

    loop {
        if shared.stop.load(Ordering::Relaxed) {
            return;
        }
        if shared.seek_pending.swap(false, Ordering::Relaxed) {
            let f = shared.seek_frame.load(Ordering::Relaxed);
            source.seek(f);
            shared.decoded_pos.store(f, Ordering::SeqCst);
            resampler.reset();
            shared.ring.lock().unwrap().clear();
        }

        // Wait while paused or the ring is full.
        {
            let mut ring = shared.ring.lock().unwrap();
            while !shared.stop.load(Ordering::Relaxed)
                && !shared.seek_pending.load(Ordering::Relaxed)
                && (ring.len() >= target || !shared.playing.load(Ordering::Relaxed))
            {
                let (g, _) = shared.wake.wait_timeout(ring, std::time::Duration::from_millis(100)).unwrap();
                ring = g;
                if shared.stop.load(Ordering::Relaxed) {
                    return;
                }
            }
        }
        if shared.stop.load(Ordering::Relaxed) || shared.seek_pending.load(Ordering::Relaxed) {
            continue;
        }

        // When looping within an explicit loop region, don't read past loop_end.
        let looping = shared.looping.load(Ordering::Relaxed);
        let region = at9_loop_region(&source, &shared);
        let pos = shared.decoded_pos.load(Ordering::SeqCst);
        let mut want_frames = scratch.len() / src_ch;
        if looping {
            if let Some((_, le)) = region {
                if pos <= le {
                    want_frames = want_frames.min((le + 1 - pos) as usize);
                }
            }
        }
        let n = source.read(&mut scratch[..want_frames * src_ch]);
        if n == 0 {
            // End of stream.
            if looping {
                let target_frame = region.map(|(ls, _)| ls).unwrap_or(0);
                source.seek(target_frame);
                shared.decoded_pos.store(target_frame, Ordering::SeqCst);
                resampler.reset();
                continue;
            }
            shared.finished.store(true, Ordering::SeqCst);
            shared.playing.store(false, Ordering::SeqCst);
            continue;
        }
        shared.decoded_pos.fetch_add((n / src_ch) as u64, Ordering::SeqCst);

        // Resample this chunk to the device rate + channel layout, push to the ring.
        let mut out = Vec::new();
        resampler.process(&scratch[..n], src_ch, dev_ch, step, &mut out);
        {
            let mut ring = shared.ring.lock().unwrap();
            ring.extend(out);
        }

        // Loop seam: if we've reached loop_end, seek back for the next read.
        if looping {
            if let Some((ls, le)) = region {
                if shared.decoded_pos.load(Ordering::SeqCst) > le {
                    source.seek(ls);
                    shared.decoded_pos.store(ls, Ordering::SeqCst);
                }
            }
        }
    }
}

/// The loop region for the current source (AT9 files only; the chime doesn't loop).
fn at9_loop_region(source: &Source, _shared: &Shared) -> Option<(u64, u64)> {
    match source {
        Source::At9(d) => d.loop_region(),
        Source::Mem { .. } => None,
    }
}

/// Linear-interpolating resampler with one frame of carry across chunk boundaries.
struct Resampler {
    phase: f64,
    prev: Vec<f32>,
    have_prev: bool,
}

impl Resampler {
    fn new(src_ch: usize) -> Self {
        Resampler { phase: 0.0, prev: vec![0.0; src_ch], have_prev: false }
    }
    fn reset(&mut self) {
        self.phase = 0.0;
        self.have_prev = false;
    }
    /// Append resampled device frames for `src` (interleaved i16) to `out`. `pos`
    /// (`self.phase`) is the read position in source frames, where index -1 is the
    /// previous chunk's last frame (`self.prev`), so interpolation is continuous
    /// across chunk boundaries.
    fn process(&mut self, src: &[i16], src_ch: usize, dev_ch: usize, step: f64, out: &mut Vec<f32>) {
        let frames = src.len() / src_ch;
        if frames == 0 {
            return;
        }
        let sample = |i: isize, c: usize| -> f32 {
            if i < 0 {
                if self.have_prev {
                    self.prev[c]
                } else {
                    src[c] as f32 / 32768.0
                }
            } else {
                src[(i as usize).min(frames - 1) * src_ch + c] as f32 / 32768.0
            }
        };
        let mut pos = self.phase;
        if !self.have_prev && pos < 0.0 {
            pos = 0.0;
        }
        // Emit while both surrounding frames are available (i in [-1, frames-2]).
        while (pos.floor() as isize) + 1 <= (frames - 1) as isize {
            let i = pos.floor() as isize;
            let frac = (pos - pos.floor()) as f32;
            for c in 0..dev_ch {
                let sc = if src_ch == 1 { 0 } else { c.min(src_ch - 1) };
                let a = sample(i, sc);
                let b = sample(i + 1, sc);
                out.push(a + (b - a) * frac);
            }
            pos += step;
        }
        // Next chunk's index -1 is this chunk's last frame.
        self.phase = pos - frames as f64;
        for c in 0..src_ch {
            self.prev[c] = src[(frames - 1) * src_ch + c] as f32 / 32768.0;
        }
        self.have_prev = true;
    }
}

fn build<T>(device: &cpal::Device, config: cpal::StreamConfig, shared: Arc<Shared>) -> Result<cpal::Stream, String>
where
    T: cpal::SizedSample + cpal::FromSample<f32>,
{
    let callback = move |data: &mut [T], _: &cpal::OutputCallbackInfo| {
        let silence = T::from_sample(0.0f32);
        if !shared.playing.load(Ordering::Relaxed) {
            data.iter_mut().for_each(|s| *s = silence);
            return;
        }
        let mut ring = shared.ring.lock().unwrap();
        for s in data.iter_mut() {
            *s = match ring.pop_front() {
                Some(v) => T::from_sample(v),
                None => silence,
            };
        }
        drop(ring);
        shared.wake.notify_all();
    };
    device
        .build_output_stream::<T, _, _>(config, callback, |e| eprintln!("audio stream error: {e}"), None)
        .map_err(|e| format!("Audio output error: {e}"))
}

pub fn format_time(secs: f64) -> String {
    let s = secs.max(0.0).round() as u64;
    format!("{:02}:{:02}", s / 60, s % 60)
}

// ---------------------------------------------------------------- done chime

/// Parse a 16-bit PCM WAV (the bundled chime) into an in-memory track.
fn parse_wav16(bytes: &[u8]) -> Option<Track> {
    if bytes.len() < 12 || &bytes[..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return None;
    }
    let (mut pos, mut channels, mut rate, mut data) = (12usize, 0u16, 0u32, None);
    while pos + 8 <= bytes.len() {
        let id = &bytes[pos..pos + 4];
        let len = u32::from_le_bytes(bytes[pos + 4..pos + 8].try_into().ok()?) as usize;
        let body = bytes.get(pos + 8..pos + 8 + len)?;
        if id == b"fmt " && body.len() >= 16 {
            channels = u16::from_le_bytes([body[2], body[3]]);
            rate = u32::from_le_bytes(body[4..8].try_into().ok()?);
        } else if id == b"data" {
            data = Some(body);
        }
        pos += 8 + len + (len & 1);
    }
    let samples: Vec<i16> = data?.chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect();
    let total = if channels == 0 { 0 } else { (samples.len() / channels as usize) as u64 };
    Some(Track {
        channels,
        sample_rate: rate,
        total_frames: total,
        loop_region: None,
        bitrate_kbps: 0,

        kind: TrackSource::Pcm(Arc::new(samples)),
    })
}

thread_local! {
    static CHIME: std::cell::RefCell<Option<Player>> = const { std::cell::RefCell::new(None) };
}

/// v2 `play_done_sound`: a short chime when a long task finishes.
pub fn play_done_sound() {
    static WAV: &[u8] = include_bytes!("../../../Assets/Sounds/done.wav");
    let Some(track) = parse_wav16(WAV) else { return };
    if let Ok(player) = Player::new(Arc::new(track), false) {
        player.play();
        // Keep the stream alive until it has played (replaced by the next chime).
        CHIME.with(|c| *c.borrow_mut() = Some(player));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_at9() -> Vec<u8> {
        let mut pcm = Vec::new();
        for i in 0..96_000 {
            let v = (f32::sin(i as f32 / 48000.0 * 440.0 * std::f32::consts::TAU) * 10000.0) as i16;
            pcm.push(v);
            pcm.push(v);
        }
        at9codec::encode::encode_pcm16(&pcm, 2, 48000, &at9codec::encode::EncodeOptions::default()).unwrap()
    }

    #[test]
    fn resampler_passthrough_identity() {
        // At step 1.0 the resampler reproduces the input (interleaved stereo).
        let mut r = Resampler::new(2);
        let src: Vec<i16> = (0..200).map(|i| (i * 100) as i16).collect();
        let mut out = Vec::new();
        r.process(&src, 2, 2, 1.0, &mut out);
        // First output frame equals the first source frame.
        assert!((out[0] - src[0] as f32 / 32768.0).abs() < 1e-6);
        assert!((out[1] - src[1] as f32 / 32768.0).abs() < 1e-6);
        // 100 input frames at step 1.0 → ~99 output frames (stereo interleaved).
        assert!(out.len() >= 98 * 2 && out.len() <= 100 * 2, "got {} samples", out.len());
        // The 10th output frame equals the 10th input frame (exact at step 1.0).
        assert!((out[10 * 2] - src[10 * 2] as f32 / 32768.0).abs() < 1e-6);
    }

    #[test]
    #[ignore = "needs an audio output device"]
    fn streaming_player_advances() {
        let at9 = make_at9();
        let track = {
            let container = at9codec::parse(&at9).unwrap();
            let mut sd = at9codec::StreamingDecoder::from_container(&container, &at9).unwrap();
            let (c, r, t, lr) = (sd.channels(), sd.sample_rate(), sd.total_frames(), sd.loop_region());
            let _ = sd.read(&mut [0i16; 4]);
            Track { channels: c, sample_rate: r, total_frames: t, loop_region: lr, bitrate_kbps: 0, kind: TrackSource::At9(Arc::new(at9)) }
        };
        let p = Player::new(Arc::new(track), false).unwrap();
        p.play();
        std::thread::sleep(std::time::Duration::from_millis(600));
        assert!(p.position_secs() > 0.1, "position did not advance: {}", p.position_secs());
    }

    #[test]
    fn chime_parses() {
        let t = super::parse_wav16(include_bytes!("../../../Assets/Sounds/done.wav")).unwrap();
        assert_eq!((t.channels, t.sample_rate), (1, 22_050));
        assert!(t.total_frames > 1000);
    }
}
