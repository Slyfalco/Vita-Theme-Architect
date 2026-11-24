//! Print the parsed theme_data of each theme.xml given on the command line as JSON
//! lines (used to compare against the v2 app on real themes).
fn main() {
    for arg in std::env::args().skip(1) {
        let d = vta_core::theme::parse_theme_xml(std::path::Path::new(&arg));
        println!("{}", serde_json::to_string(&d).unwrap());
    }
}
