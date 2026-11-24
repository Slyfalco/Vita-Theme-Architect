//! Dev check: counts and the first theme's title (no URLs printed).
fn main() {
    println!("status: {:?}", vta_online::status());
    match vta_online::count("", vta_online::Field::Title, vta_online::Filter::All, true) {
        Ok(n) => println!("themes: {n}"),
        Err(e) => println!("error: {e}"),
    }
    if let Ok(page) = vta_online::get("", vta_online::Field::Title, vta_online::Filter::All, vta_online::Sort::Downloads, 1, 3, false) {
        for e in page {
            let keys: Vec<&String> = e.0.keys().collect();
            println!("{} by {} | {} downloads | keys: {:?}", e.str("title"), e.str("author"), e.num("downloads"), keys);
        }
    }
}
