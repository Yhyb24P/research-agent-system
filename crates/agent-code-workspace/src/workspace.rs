use sha2::{Digest, Sha256};
use std::io::Write;
use std::path::{Component, Path, PathBuf};

use crate::error::ToolError;

/// A workspace root that all file access is contained to.
#[derive(Debug, Clone)]
pub struct Workspace {
    root: PathBuf,
    root_canonical: PathBuf,
}

impl Workspace {
    /// Canonicalize `root` and prepare a contained workspace.
    pub fn new(root: impl AsRef<Path>) -> Result<Self, ToolError> {
        let root_canonical =
            std::fs::canonicalize(root.as_ref()).map_err(|e| ToolError::Io(e.to_string()))?;
        Ok(Self {
            root: root_canonical.clone(),
            root_canonical,
        })
    }

    /// The canonical workspace root.
    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Resolve an existing file path, contained to the workspace.
    pub fn resolve(&self, rel: &str) -> Result<PathBuf, ToolError> {
        let joined = self.contained(rel)?;
        let canon = std::fs::canonicalize(&joined).map_err(|e| ToolError::Io(e.to_string()))?;
        if !canon.starts_with(&self.root_canonical) {
            return Err(ToolError::SymlinkEscape(rel.into()));
        }
        Ok(canon)
    }

    /// Resolve a write target whose parent must exist and be contained. The
    /// target itself may not exist yet.
    pub fn resolve_target(&self, rel: &str) -> Result<PathBuf, ToolError> {
        let joined = self.contained(rel)?;
        let parent = joined
            .parent()
            .filter(|p| !p.as_os_str().is_empty())
            .ok_or_else(|| ToolError::Io("no parent directory".into()))?;
        let parent_canon =
            std::fs::canonicalize(parent).map_err(|e| ToolError::Io(e.to_string()))?;
        if !parent_canon.starts_with(&self.root_canonical) {
            return Err(ToolError::SymlinkEscape(rel.into()));
        }
        let name = joined
            .file_name()
            .ok_or_else(|| ToolError::Io("no file name".into()))?;
        Ok(parent_canon.join(name))
    }

    /// SHA-256 hex digest of a contained file.
    pub fn hash_file(&self, rel: &str) -> Result<String, ToolError> {
        let bytes = std::fs::read(self.resolve(rel)?).map_err(|e| ToolError::Io(e.to_string()))?;
        let digest = Sha256::digest(&bytes);
        let mut hex = String::with_capacity(64);
        for b in digest.iter() {
            hex.push_str(&format!("{:02x}", b));
        }
        Ok(hex)
    }

    /// Atomically write `content` to a contained path (temp file + rename).
    pub fn atomic_write(&self, rel: &str, content: &str) -> Result<(), ToolError> {
        let target = self.resolve_target(rel)?;
        let parent = target
            .parent()
            .ok_or_else(|| ToolError::Io("no parent".into()))?;
        let name = target
            .file_name()
            .ok_or_else(|| ToolError::Io("no file name".into()))?;
        let tmp = parent.join(format!(".{}.tmp", name.to_string_lossy()));
        let file = std::fs::File::create(&tmp).map_err(|e| ToolError::Io(e.to_string()))?;
        let mut writer = std::io::BufWriter::new(file);
        writer
            .write_all(content.as_bytes())
            .and_then(|_| writer.flush())
            .map_err(|e| ToolError::Io(e.to_string()))?;
        std::fs::rename(&tmp, &target).map_err(|e| ToolError::Io(e.to_string()))?;
        Ok(())
    }

    /// Read a contained file as UTF-8.
    pub fn read_file(&self, rel: &str) -> Result<String, ToolError> {
        std::fs::read_to_string(self.resolve(rel)?).map_err(|e| ToolError::Io(e.to_string()))
    }

    /// Lexical containment check for a relative path.
    fn contained(&self, rel: &str) -> Result<PathBuf, ToolError> {
        let p = Path::new(rel);
        if p.is_absolute() || !within_root_lexically(p) {
            return Err(ToolError::PathEscape(rel.into()));
        }
        Ok(self.root.join(p))
    }
}

/// True when a relative path never rises above the current directory.
fn within_root_lexically(p: &Path) -> bool {
    let mut depth = 0i32;
    for component in p.components() {
        match component {
            Component::ParentDir => depth -= 1,
            Component::Normal(_) => depth += 1,
            Component::CurDir => {}
            Component::RootDir | Component::Prefix(_) => return false,
        }
        if depth < 0 {
            return false;
        }
    }
    true
}
