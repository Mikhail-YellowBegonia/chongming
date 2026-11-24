use std::path::PathBuf;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let manifest_dir = PathBuf::from(std::env::var("CARGO_MANIFEST_DIR")?);
    let proto_root = manifest_dir.join("../../proto");
    // Business boundary: Edge must only compile common.proto and online.proto.
    let protos = [
        proto_root.join("chongming/v1/common.proto"),
        proto_root.join("chongming/v1/online.proto"),
    ];

    for proto in &protos {
        println!("cargo:rerun-if-changed={}", proto.display());
    }

    prost_build::Config::new().compile_protos(&protos, &[&proto_root])?;
    Ok(())
}
