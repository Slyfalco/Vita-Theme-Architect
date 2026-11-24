//! Blocking HTTP with the semantics of v2's `requests` calls: the timeout is
//! per connect/read (not a total), redirects are followed (up to 30), error
//! statuses are returned rather than raised, and `raise_for_status` gives the
//! same message as `requests`.

use super::py::PyError;
use std::time::Duration;
use ureq::ResponseExt;

pub(crate) struct HttpResponse {
    pub status: u16,
    pub url: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl HttpResponse {
    /// `resp.text` (decoded leniently).
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).into_owned()
    }
    /// `resp.ok`.
    pub fn ok(&self) -> bool {
        self.status < 400
    }
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }
    /// `resp.json()`.
    pub fn json(&self) -> Result<serde_json::Value, PyError> {
        serde_json::from_slice(&self.body).map_err(|e| PyError::new("JSONDecodeError", e.to_string()))
    }
    /// `resp.raise_for_status()`.
    pub fn raise_for_status(&self) -> Result<(), PyError> {
        let reason = http_reason(self.status);
        let side = match self.status {
            400..=499 => "Client",
            500..=599 => "Server",
            _ => return Ok(()),
        };
        Err(PyError::new("HTTPError", format!("{} {side} Error: {reason} for url: {}", self.status, self.url)))
    }
}

fn http_reason(status: u16) -> &'static str {
    ureq::http::StatusCode::from_u16(status).ok().and_then(|s| s.canonical_reason()).unwrap_or("")
}

fn agent(timeout: u64) -> ureq::Agent {
    let t = Some(Duration::from_secs(timeout));
    ureq::Agent::config_builder()
        .http_status_as_error(false)
        .max_redirects(30)
        .timeout_connect(t)
        .timeout_per_read(t)
        .timeout_per_write(t)
        .build()
        .into()
}

fn net_error(e: ureq::Error) -> PyError {
    match e {
        ureq::Error::Timeout(_) => PyError::new("ReadTimeout", e.to_string()),
        other => PyError::new("ConnectionError", other.to_string()),
    }
}

fn finish(resp: Result<ureq::http::Response<ureq::Body>, ureq::Error>, url: &str) -> Result<HttpResponse, PyError> {
    let mut resp = resp.map_err(net_error)?;
    let final_url = resp.get_uri().to_string();
    let headers = resp
        .headers()
        .iter()
        .map(|(k, v)| (k.as_str().to_string(), String::from_utf8_lossy(v.as_bytes()).into_owned()))
        .collect();
    let status = resp.status().as_u16();
    let body = resp.body_mut().with_config().limit(u64::MAX).read_to_vec().map_err(net_error)?;
    Ok(HttpResponse { status, url: if final_url.is_empty() { url.to_string() } else { final_url }, headers, body })
}

/// `requests.get(url, headers=..., timeout=...)`.
pub(crate) fn get(url: &str, headers: &[(&str, &str)], timeout: u64) -> Result<HttpResponse, PyError> {
    let mut req = agent(timeout).get(url);
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    finish(req.call(), url)
}

/// `requests.post(url, json=body, headers=..., timeout=...)`: the body is
/// serialised like `requests` does (`json.dumps` defaults).
pub(crate) fn post_json(url: &str, headers: &[(&str, &str)], body: &serde_json::Value, timeout: u64) -> Result<HttpResponse, PyError> {
    let mut req = agent(timeout).post(url);
    for (k, v) in headers {
        req = req.header(*k, *v);
    }
    finish(req.send(super::py::json_dumps(body)), url)
}
