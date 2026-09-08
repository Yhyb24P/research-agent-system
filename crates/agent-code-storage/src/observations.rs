//! Append-only durable observations. Records are never compacted or deleted
//! here; the context layer only reads them to build a bounded projection.

use agent_code_model::Observation;
use rusqlite::params;

use crate::journal::{now, SqliteJournal};

impl SqliteJournal {
    /// Append one observation to this session's durable log.
    pub fn append_observation(&self, obs: &Observation) -> Result<(), rusqlite::Error> {
        let sid = self.session().as_str();
        self.conn().execute(
            "INSERT INTO observations (session_id, kind, payload, created_at)
                 VALUES (?1, ?2, ?3, ?4)",
            params![sid, obs.kind(), obs.encode(), now()],
        )?;
        Ok(())
    }

    /// Read this session's observations in insertion order.
    pub fn read_observations(&self) -> Result<Vec<Observation>, rusqlite::Error> {
        let sid = self.session().as_str();
        let mut stmt = self
            .conn()
            .prepare("SELECT payload FROM observations WHERE session_id = ?1 ORDER BY id ASC")?;
        let rows = stmt.query_map(params![sid], |row| row.get::<_, String>(0))?;
        let mut out = Vec::new();
        for row in rows {
            let payload = row?;
            out.push(decode_payload(&payload)?);
        }
        Ok(out)
    }
}

fn decode_payload(payload: &str) -> Result<Observation, rusqlite::Error> {
    Observation::decode(payload).map_err(|e| {
        rusqlite::Error::FromSqlConversionFailure(
            0,
            rusqlite::types::Type::Text,
            Box::new(std::io::Error::new(std::io::ErrorKind::InvalidData, e)),
        )
    })
}

#[cfg(test)]
mod tests {
    use agent_code_core::{AgentState, Journal, SessionId};
    use agent_code_model::Observation;
    use rusqlite::Connection;

    use super::SqliteJournal;

    fn temp_db(name: &str) -> std::path::PathBuf {
        std::env::temp_dir().join(format!("agent_code_obs_{name}_{}.db", std::process::id()))
    }

    #[test]
    fn observations_survive_reopen() {
        let path = temp_db("reopen");
        let _ = std::fs::remove_file(&path);
        {
            let conn = Connection::open(&path).expect("open db");
            let mut j = SqliteJournal::open(conn, SessionId::new("s")).expect("bind");
            j.create(AgentState::Initializing).expect("create");
            j.append_observation(&Observation::Command {
                program: "cargo".into(),
                argv: vec!["test".into()],
                exit: Some(0),
            })
            .expect("append 1");
            j.append_observation(&Observation::Error {
                signature: "E0308".into(),
                count: 2,
            })
            .expect("append 2");
        }
        // Reopen the same file: both records are still present, in order.
        let conn = Connection::open(&path).expect("reopen db");
        let j = SqliteJournal::open(conn, SessionId::new("s")).expect("bind");
        let obs = j.read_observations().expect("read");
        assert_eq!(
            obs,
            vec![
                Observation::Command {
                    program: "cargo".into(),
                    argv: vec!["test".into()],
                    exit: Some(0),
                },
                Observation::Error {
                    signature: "E0308".into(),
                    count: 2,
                },
            ]
        );
        let _ = std::fs::remove_file(&path);
    }

    #[test]
    fn observations_are_session_scoped() {
        let path = temp_db("scoped");
        let _ = std::fs::remove_file(&path);
        let c1 = Connection::open(&path).expect("open a");
        let c2 = Connection::open(&path).expect("open b");
        let mut a = SqliteJournal::open(c1, SessionId::new("a")).expect("bind a");
        let b = SqliteJournal::open(c2, SessionId::new("b")).expect("bind b");
        a.create(AgentState::Initializing).expect("create a");
        a.append_observation(&Observation::Text("only in a".into()))
            .expect("append");
        assert_eq!(a.read_observations().expect("read a").len(), 1);
        assert!(b.read_observations().expect("read b").is_empty());
        let _ = std::fs::remove_file(&path);
    }
}
