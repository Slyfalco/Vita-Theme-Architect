//! The model-file lists v2's app reads from `icon_ai_worker.py` (via `_worker_module`):
//! `model_files`, `_helper_model_files`, `flux2_files`, `flux2_key_files`,
//! `_flux2_full`, `FLUX2_REPO`/`FLUX2_BNB_REPO` and `BACKENDS[...]['label']`.
//!
//! v3 can't import the Python worker in-process, so these are a static copy. They
//! MUST stay identical to the worker's tables (the worker still downloads the files);
//! the `matches_python_worker` test checks every combination against output produced
//! by the real `icon_ai_worker.py`. Regenerate `testdata/py_vectors.json` if the
//! worker's lists change.

pub type RepoFile = (String, String);

pub const BASE_MODEL: &str = "stable-diffusion-v1-5/stable-diffusion-v1-5";
pub const SDXL_MODEL: &str = "cagliostrolab/animagine-xl-4.0";
pub const SDXL_CONTROLNET: &str = "diffusers/controlnet-canny-sdxl-1.0-small";
pub const SDXL_IP_WEIGHTS: &str = "ip-adapter_sdxl_vit-h.safetensors";
pub const SDXL_OPENPOSE: &str = "xinsir/controlnet-openpose-sdxl-1.0";
pub const SDXL_IP_PLUS_WEIGHTS: &str = "ip-adapter-plus_sdxl_vit-h.safetensors";
pub const CONTROLNET_MODEL: &str = "lllyasviel/control_v11p_sd15_canny";
pub const IP_ADAPTER_REPO: &str = "h94/IP-Adapter";
pub const IP_ADAPTER_WEIGHTS: &str = "ip-adapter_sd15.safetensors";
pub const OPENPOSE_MODEL: &str = "lllyasviel/control_v11p_sd15_openpose";
pub const IP_ADAPTER_PLUS_WEIGHTS: &str = "ip-adapter-plus_sd15.safetensors";
pub const FLUX2_REPO: &str = "black-forest-labs/FLUX.2-dev";
pub const FLUX2_BNB_REPO: &str = "diffusers/FLUX.2-dev-bnb-4bit";
pub const SEGMENT_REPO: &str = "skytnt/anime-seg";
pub const SEGMENT_FILE: &str = "isnetis.onnx";
pub const TEXT_DET_REPO: &str = "SWHL/RapidOCR";
pub const TEXT_DET_FILE: &str = "PP-OCRv4/ch_PP-OCRv4_det_infer.onnx";
pub const HEAD_REPO: &str = "deepghs/anime_head_detection";
pub const HEAD_FILE: &str = "head_detect_v0.5_s_pruned/model.onnx";
pub const FACE_REPO: &str = "deepghs/anime_face_detection";
pub const FACE_FILE: &str = "face_detect_v1.4_n/model.onnx";
pub const PERSON_REPO: &str = "deepghs/anime_person_detection";
pub const PERSON_FILE: &str = "person_detect_v1.3_s/model.onnx";
pub const ART_STYLE_REPO: &str = "deepghs/anime_classification";
pub const ART_STYLE_FILE: &str = "mobilenetv3_v1.5_dist/model.onnx";
pub const TAGGER_REPO: &str = "SmilingWolf/wd-vit-tagger-v3";
pub const IDENTITY_REPO: &str = "deepghs/ccip_onnx";
pub const IDENTITY_DIR: &str = "ccip-caformer-24-randaug-pruned";

/// Worker `EVENT_PREFIX` (`@@VTA `): stdout lines carrying JSON events.
pub const EVENT_PREFIX: &str = "@@VTA ";

fn rf(repo: &str, path: impl Into<String>) -> RepoFile {
    (repo.to_string(), path.into())
}

/// Port of worker `model_files(half, segmenter, poses, model, skip_base)`: every
/// model file a generation needs, as (repo, path). Any model other than `sdxl`
/// uses the SD 1.5 list (as in v2).
pub fn model_files(half: bool, segmenter: bool, poses: bool, model: &str, skip_base: bool) -> Vec<RepoFile> {
    let v = if half { ".fp16" } else { "" };
    let (mut files, base_repo) = if model == "sdxl" {
        let mut files: Vec<RepoFile> = [
            "model_index.json", "scheduler/scheduler_config.json",
            "tokenizer/merges.txt", "tokenizer/special_tokens_map.json", "tokenizer/tokenizer_config.json", "tokenizer/vocab.json",
            "tokenizer_2/merges.txt", "tokenizer_2/special_tokens_map.json", "tokenizer_2/tokenizer_config.json", "tokenizer_2/vocab.json",
            "text_encoder/config.json", "text_encoder/model.safetensors",
            "text_encoder_2/config.json", "text_encoder_2/model.safetensors",
            "unet/config.json", "unet/diffusion_pytorch_model.safetensors",
            "vae/config.json", "vae/diffusion_pytorch_model.safetensors",
        ]
        .iter()
        .map(|p| rf(SDXL_MODEL, *p))
        .collect();
        files.push(rf(SDXL_CONTROLNET, "config.json"));
        files.push(rf(SDXL_CONTROLNET, format!("diffusion_pytorch_model{v}.safetensors")));
        files.push(rf(IP_ADAPTER_REPO, format!("sdxl_models/{SDXL_IP_WEIGHTS}")));
        files.push(rf(IP_ADAPTER_REPO, "models/image_encoder/config.json"));
        files.push(rf(IP_ADAPTER_REPO, "models/image_encoder/model.safetensors"));
        if poses {
            files.push(rf(SDXL_OPENPOSE, "config.json"));
            files.push(rf(SDXL_OPENPOSE, "diffusion_pytorch_model.safetensors"));
            files.push(rf(IP_ADAPTER_REPO, format!("sdxl_models/{SDXL_IP_PLUS_WEIGHTS}")));
        }
        (files, SDXL_MODEL)
    } else {
        let mut files: Vec<RepoFile> = [
            "model_index.json".to_string(), "scheduler/scheduler_config.json".into(),
            "feature_extractor/preprocessor_config.json".into(),
            "tokenizer/merges.txt".into(), "tokenizer/special_tokens_map.json".into(), "tokenizer/tokenizer_config.json".into(),
            "tokenizer/vocab.json".into(), "text_encoder/config.json".into(), format!("text_encoder/model{v}.safetensors"),
            "unet/config.json".into(), format!("unet/diffusion_pytorch_model{v}.safetensors"),
            "vae/config.json".into(), format!("vae/diffusion_pytorch_model{v}.safetensors"),
        ]
        .into_iter()
        .map(|p| rf(BASE_MODEL, p))
        .collect();
        files.push(rf(CONTROLNET_MODEL, "config.json"));
        files.push(rf(CONTROLNET_MODEL, format!("diffusion_pytorch_model{v}.safetensors")));
        files.push(rf(IP_ADAPTER_REPO, format!("models/{IP_ADAPTER_WEIGHTS}")));
        files.push(rf(IP_ADAPTER_REPO, "models/image_encoder/config.json"));
        files.push(rf(IP_ADAPTER_REPO, "models/image_encoder/model.safetensors"));
        if poses {
            files.push(rf(OPENPOSE_MODEL, "config.json"));
            files.push(rf(OPENPOSE_MODEL, format!("diffusion_pytorch_model{v}.safetensors")));
            files.push(rf(IP_ADAPTER_REPO, format!("models/{IP_ADAPTER_PLUS_WEIGHTS}")));
        }
        (files, BASE_MODEL)
    };
    if skip_base {
        files.retain(|(r, _)| r != base_repo);
    }
    files.extend(helper_model_files(segmenter));
    files
}

/// Port of worker `_helper_model_files(segmenter)`: the small detector/identity models.
pub fn helper_model_files(segmenter: bool) -> Vec<RepoFile> {
    let mut files = Vec::new();
    if segmenter {
        files.push(rf(SEGMENT_REPO, SEGMENT_FILE));
    }
    files.push(rf(TEXT_DET_REPO, TEXT_DET_FILE));
    files.push(rf(HEAD_REPO, HEAD_FILE));
    files.push(rf(FACE_REPO, FACE_FILE));
    files.push(rf(PERSON_REPO, PERSON_FILE));
    files.push(rf(ART_STYLE_REPO, ART_STYLE_FILE));
    files.push(rf(TAGGER_REPO, "model.onnx"));
    files.push(rf(TAGGER_REPO, "selected_tags.csv"));
    files.push(rf(IDENTITY_REPO, format!("{IDENTITY_DIR}/model_feat.onnx")));
    files.push(rf(IDENTITY_REPO, format!("{IDENTITY_DIR}/model_metrics.onnx")));
    files
}

/// Port of worker `_flux2_full(variant)`: true for the full bf16 build.
pub fn flux2_full(variant: &str) -> bool {
    variant == "flux2_full"
}

/// The HF repo holding a FLUX.2 variant.
pub fn flux2_repo(variant: &str) -> &'static str {
    if flux2_full(variant) { FLUX2_REPO } else { FLUX2_BNB_REPO }
}

const FLUX2_PATTERNS: [&str; 6] = ["transformer/*", "text_encoder/*", "vae/*", "scheduler/*", "tokenizer*/*", "model_index.json"];

/// Port of worker `flux2_files(variant)`: (repo, allow_patterns).
pub fn flux2_files(variant: &str) -> Vec<(String, Vec<String>)> {
    vec![(flux2_repo(variant).to_string(), FLUX2_PATTERNS.iter().map(|s| s.to_string()).collect())]
}

/// Port of worker `flux2_key_files(variant)`.
pub fn flux2_key_files(variant: &str) -> Vec<RepoFile> {
    let repo = flux2_repo(variant);
    vec![rf(repo, "transformer/config.json"), rf(repo, "text_encoder/config.json"), rf(repo, "vae/config.json")]
}

/// Worker `BACKENDS[variant]['label']` (v2 `_flux2_label`: falls back to the key).
pub fn backend_label(key: &str) -> String {
    match key {
        "sd15" => "Standard (SD 1.5)",
        "sdxl" => "High quality (SDXL)",
        "flux2" => "Ultra (FLUX.2 4-bit, local)",
        "flux2_full" => "Ultra+ (FLUX.2 full, local)",
        other => other,
    }
    .to_string()
}

/// Port of worker `detect_base_arch(path)`: 'sd15' or 'sdxl' for a user checkpoint
/// (a `.safetensors` file or a diffusers folder); 'sdxl' when unsure.
pub fn detect_base_arch(path: &std::path::Path) -> &'static str {
    use std::io::Read;
    if path.is_dir() {
        return if path.join("text_encoder_2").is_dir() { "sdxl" } else { "sd15" };
    }
    let header = (|| -> Option<serde_json::Map<String, serde_json::Value>> {
        let mut f = std::fs::File::open(path).ok()?;
        let mut len = [0u8; 8];
        f.read_exact(&mut len).ok()?;
        let mut buf = Vec::new();
        f.take(u64::from_le_bytes(len)).read_to_end(&mut buf).ok()?;
        match serde_json::from_slice(&buf).ok()? {
            serde_json::Value::Object(m) => Some(m),
            _ => None,
        }
    })();
    let Some(header) = header else { return "sdxl" };
    if header
        .keys()
        .any(|k| k.starts_with("conditioner.embedders.1") || k.contains("label_emb") || k.contains("add_embedding"))
    {
        return "sdxl";
    }
    if header.keys().any(|k| k.starts_with("cond_stage_model.transformer") || k.starts_with("text_model.")) {
        return "sd15";
    }
    "sdxl"
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn pairs(v: &Value) -> Vec<RepoFile> {
        v.as_array()
            .unwrap()
            .iter()
            .map(|p| (p[0].as_str().unwrap().to_string(), p[1].as_str().unwrap().to_string()))
            .collect()
    }

    #[test]
    fn matches_python_worker() {
        let v: Value = serde_json::from_str(include_str!("testdata/py_vectors.json")).unwrap();
        let mf = v["model_files"].as_object().unwrap();
        assert_eq!(mf.len(), 48);
        for (key, expected) in mf {
            let b = key.as_bytes();
            let (half, seg, poses) = (b[0] == b'1', b[1] == b'1', b[2] == b'1');
            let model = &key[3..key.len() - 1];
            let skip = key.ends_with('1');
            assert_eq!(model_files(half, seg, poses, model, skip), pairs(expected), "{key}");
        }
        for s in [false, true] {
            let k = if s { "True" } else { "False" };
            assert_eq!(helper_model_files(s), pairs(&v["helpers"][k]));
        }
        for variant in ["flux2", "flux2_full"] {
            assert_eq!(flux2_key_files(variant), pairs(&v["flux2_key_files"][variant]));
            let f = &v["flux2_files"][variant][0];
            let ours = &flux2_files(variant)[0];
            assert_eq!(ours.0, f[0].as_str().unwrap());
            let pats: Vec<String> = f[1].as_array().unwrap().iter().map(|x| x.as_str().unwrap().to_string()).collect();
            assert_eq!(ours.1, pats);
        }
    }
}
