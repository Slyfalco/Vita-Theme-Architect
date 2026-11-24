//! Fernet (the `cryptography.fernet` token format v2 encrypts its calibration data
//! with): `0x80 | u64 BE timestamp | 16-byte IV | AES-128-CBC(PKCS7) | HMAC-SHA256`.
//! v2 stores tokens as their raw (base64-decoded) bytes, so these functions work on
//! raw tokens. Wire-compatible both ways with Python's implementation.

use aes::Aes128;
use base64::engine::general_purpose::{GeneralPurpose, GeneralPurposeConfig};
use base64::engine::DecodePaddingMode;
use base64::{alphabet, Engine};
use cbc::cipher::{block_padding::Pkcs7, BlockDecryptMut, BlockEncryptMut, KeyIvInit};
use hmac::{Hmac, Mac};
use sha2::Sha256;

const VERSION: u8 = 0x80;

fn urlsafe_lenient() -> GeneralPurpose {
    GeneralPurpose::new(
        &alphabet::URL_SAFE,
        GeneralPurposeConfig::new().with_decode_padding_mode(DecodePaddingMode::Indifferent),
    )
}

/// `base64.urlsafe_b64encode` (padded).
pub fn urlsafe_b64encode(data: &[u8]) -> String {
    base64::engine::general_purpose::URL_SAFE.encode(data)
}

#[derive(Clone)]
pub struct Fernet {
    signing: [u8; 16],
    encryption: [u8; 16],
}

impl Fernet {
    /// `Fernet(key)`: `key` is the url-safe base64 of 32 bytes. `None` if invalid
    /// (Python raises `ValueError`).
    pub fn new(key_b64: &[u8]) -> Option<Self> {
        let text: Vec<u8> = key_b64.iter().copied().filter(|b| !b.is_ascii_whitespace()).collect();
        let raw = urlsafe_lenient().decode(text).ok()?;
        Self::from_raw(&raw)
    }

    pub fn from_raw(raw: &[u8]) -> Option<Self> {
        if raw.len() != 32 {
            return None;
        }
        let mut signing = [0u8; 16];
        let mut encryption = [0u8; 16];
        signing.copy_from_slice(&raw[..16]);
        encryption.copy_from_slice(&raw[16..]);
        Some(Fernet { signing, encryption })
    }

    /// `Fernet.generate_key()`: url-safe base64 of 32 random bytes.
    pub fn generate_key() -> String {
        let mut raw = [0u8; 32];
        getrandom::getrandom(&mut raw).expect("system random source");
        urlsafe_b64encode(&raw)
    }

    /// Encrypt to a raw token (current time, random IV).
    pub fn encrypt_raw(&self, data: &[u8]) -> Vec<u8> {
        let mut iv = [0u8; 16];
        getrandom::getrandom(&mut iv).expect("system random source");
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs())
            .unwrap_or(0);
        self.encrypt_with(data, now, iv)
    }

    fn encrypt_with(&self, data: &[u8], time: u64, iv: [u8; 16]) -> Vec<u8> {
        let ct = cbc::Encryptor::<Aes128>::new(&self.encryption.into(), &iv.into())
            .encrypt_padded_vec_mut::<Pkcs7>(data);
        let mut out = Vec::with_capacity(1 + 8 + 16 + ct.len() + 32);
        out.push(VERSION);
        out.extend_from_slice(&time.to_be_bytes());
        out.extend_from_slice(&iv);
        out.extend_from_slice(&ct);
        let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(&self.signing).expect("HMAC accepts any key size");
        mac.update(&out);
        out.extend_from_slice(&mac.finalize().into_bytes());
        out
    }

    /// Decrypt a raw token (no TTL check, like `decrypt(token)` without `ttl`).
    /// `None` = `InvalidToken` (bad version, tampered, wrong key, bad padding).
    pub fn decrypt_raw(&self, token: &[u8]) -> Option<Vec<u8>> {
        if token.len() < 1 + 8 + 16 + 32 || token[0] != VERSION {
            return None;
        }
        let (body, tag) = token.split_at(token.len() - 32);
        let mut mac = <Hmac<Sha256> as Mac>::new_from_slice(&self.signing).ok()?;
        mac.update(body);
        mac.verify_slice(tag).ok()?;
        let iv: [u8; 16] = body[9..25].try_into().ok()?;
        let ct = &body[25..];
        if ct.is_empty() || ct.len() % 16 != 0 {
            return None;
        }
        cbc::Decryptor::<Aes128>::new(&self.encryption.into(), &iv.into())
            .decrypt_padded_vec_mut::<Pkcs7>(ct)
            .ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_and_tamper() {
        let key = Fernet::generate_key();
        let f = Fernet::new(key.as_bytes()).unwrap();
        let tok = f.encrypt_raw(b"hello calibration");
        assert_eq!(f.decrypt_raw(&tok).unwrap(), b"hello calibration");
        let mut bad = tok.clone();
        bad[30] ^= 1;
        assert!(f.decrypt_raw(&bad).is_none());
        let other = Fernet::new(Fernet::generate_key().as_bytes()).unwrap();
        assert!(other.decrypt_raw(&tok).is_none());
        assert!(Fernet::new(b"not a key").is_none());
    }
}
