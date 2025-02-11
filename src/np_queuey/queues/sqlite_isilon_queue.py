"""
Base class for sqlite job queues.

- file must be accessible on //allen (has implications for sqlite, e.g.
  incompat with 'wal' mode)
  
>>> q = SqliteJobQueue(table_name='test')
>>> q['123456789_366122_20230422'] = get_job('123456789_366122_20230422')
>>> assert q.next().session == np_session.Session('123456789_366122_20230422')
>>> q.add_or_update('123456789_366122_20230422', priority=99)
>>> import datetime; assert datetime.datetime.fromtimestamp(q['123456789_366122_20230422'].added)
>>> q.update('123456789_366122_20230422', finished=0)
>>> assert q['123456789_366122_20230422'].priority == 99
>>> q.set_started('123456789_366122_20230422')
>>> assert q.is_started('123456789_366122_20230422')
>>> q.set_finished('123456789_366122_20230422')
>>> assert q['123456789_366122_20230422'].finished == 1
>>> q.set_queued('123456789_366122_20230422')
>>> assert not q['123456789_366122_20230422'].finished
>>> assert not q.is_started('123456789_366122_20230422')
>>> del q['123456789_366122_20230422']
>>> assert '123456789_366122_20230422' not in q
"""
from __future__ import annotations

import collections.abc
import contextlib
import pathlib
import sqlite3
import time
from typing import Any, Generator, Iterator, Optional, Type

import np_config
import np_session

from np_queuey.types import Job, JobArgs, JobT, SessionArgs
from np_queuey.utils import JobDataclass, get_job, get_session

DEFAULT_DB_PATH = '//allen/programs/mindscope/workgroups/dynamicrouting/ben/np_queuey/.shared.db'

JOB_ARGS_TO_SQL_DEFINITIONS: dict[str, str] = {
    'session': 'TEXT PRIMARY KEY NOT NULL',
    'added': 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL',  # YYYY-MM-DD HH:MM:SS
    'priority': 'INTEGER DEFAULT 0',
    'started': 'INTEGER DEFAULT NULL',
    'hostname': 'TEXT DEFAULT NULL',
    'finished': 'INTEGER DEFAULT NULL',  # [None] 0 or 1
    'error': 'TEXT DEFAULT NULL',
}
"""Mapping of job attribute names (keys in db) to sqlite3 column definitions."""

def sql_table(column_name_to_definition_mapping: dict[str, str]) -> str:
    """
    Define table in sqlite3.
    
    >>> sql_table({'col1': 'TEXT PRIMARY KEY NOT NULL', 'col2': 'INTEGER'})
    '(col1 TEXT PRIMARY KEY NOT NULL, col2 INTEGER)'
    """
    return (
        '('
        + ', '.join(
            [
                '{} {}'.format(col, defn)
                for col, defn in column_name_to_definition_mapping.items()
            ]
        )
        + ')'
    )

class SqliteJobQueue(collections.abc.MutableMapping):
    
    db: sqlite3.Connection
    """sqlite3 db connection to shared file on Isilon"""
    
    sqlite_db_path: str | pathlib.Path = DEFAULT_DB_PATH
    table_name: str = 'sqlite_job_queue'
    
    column_definitions: dict[str, str] = JOB_ARGS_TO_SQL_DEFINITIONS
    job_type: Type[Job] = JobDataclass
    """Job class to use for the queue - see `np_queuey.types.Job` protocol for required attributes"""
    
    def __init__(
        self, 
        **kwargs,
        ) -> None:
        """
        Pass in any attributes as kwargs to assign to instance.
        """
        for key, value in kwargs.items():
            setattr(self, key, value)
        
        if not pathlib.Path(self.sqlite_db_path).exists():
            pathlib.Path(self.sqlite_db_path).parent.mkdir(parents=True, exist_ok=True)
            
        self.validate_attributes()
        self.setup_db_connection()
        self.setup_job_table()
    
    def validate_attributes(self) -> None:
        assert all(hasattr(self.job_type('test'), attr) for attr in self.column_definitions.keys()), (
            '`self.job_type` must have all attributes exactly matching keys in `self.column_definitions`.',
            f'{self.job_type("test")=} {self.column_definitions.keys()=}',
        )
        assert isinstance(self.job_type('test'), Job)
        
    def setup_db_connection(self) -> None:
        self.db = sqlite3.connect(str(self.sqlite_db_path), timeout=1)
        self.db.isolation_level = None  # autocommit mode
        self.db.execute('pragma journal_mode="delete"')
        self.db.execute('pragma synchronous=2')
        
    def setup_job_table(self) -> None:
        """
        Create table with `self.table_name` if it doesn't exist.    
        
        >>> s = SqliteJobQueue(table_name='test')
        >>> s.setup_job_table()
        >>> with s.cursor() as c:
        ...   result = c.execute('SELECT count(*) FROM sqlite_schema WHERE type="table" AND name="test"').fetchall()[0][0]
        >>> assert result == 1, f'Test result returned {result}: expected 1 (True)'
        """
        with self.cursor() as c:
            c.execute(
                f'CREATE TABLE IF NOT EXISTS {self.table_name} '
                + sql_table(self.column_definitions),
            )

    @contextlib.contextmanager
    def cursor(self) -> Generator[sqlite3.Cursor, None, None]:
        """
        >>> with SqliteJobQueue(table_name='test').cursor() as c:
        ...    assert isinstance(c, sqlite3.Cursor)
        ...    _ = c.execute('SELECT 1').fetchall()
        """
        cursor = self.db.cursor()
        try:
            cursor.execute('begin exclusive')
            yield cursor
        except Exception:
            self.db.rollback()
            raise
        else:
            self.db.commit()
        finally:
            cursor.close()
    
    def from_job(self, job: Job) -> tuple[JobArgs, ...]:
        """Convert a job to a tuple of args for inserting into sqlite."""
        job_args = []
        for attr in self.column_definitions.keys():
            if attr == 'session':
                value = np_session.Session(job.session).folder
            else:
                value = getattr(job, attr)
            job_args.append(value)    
        return tuple(job_args)
    
    def to_job(self, *args: JobArgs, **kwargs: JobArgs) -> JobT:
        """Convert args or kwargs into a job.
        
        If args are provided, the assumption is they came from sqlite in the
        order specified by `self.column_definitions`.
        """
        if args and kwargs:
            raise ValueError(f'Cannot pass both args and kwargs: {args=}, {kwargs=}')
        if args:
            kwargs = dict(zip(self.column_definitions.keys(), args))
        return self.job_type(**kwargs)

    def __getitem__(self, session_or_job: SessionArgs | Job) -> JobT:
        """Get a job from the queue, matching on session."""
        session = get_session(session_or_job)
        with self.cursor() as c:
            hits = c.execute(
                f'SELECT * FROM {self.table_name} WHERE session = ?',
                (session.folder,),
            ).fetchall()
        if not hits:
            raise KeyError(session)
        if len(hits) > 1:
            raise ValueError(f'Found multiple jobs for {session=}. Expected `session` to be unique.')
        return self.to_job(*hits[0])
        
    def __setitem__(self, session_or_job: SessionArgs | Job, job: Job) -> None:
        """Add a job to the queue or update the existing entry."""
        session = get_session(session_or_job)
        if session != job.session:
            raise ValueError(f'`session` values don"t match {session_or_job=}, {job=}')
        with self.cursor() as c:
            c.execute(
                (
                    f'INSERT OR REPLACE INTO {self.table_name} (' +
                    ', '.join(self.column_definitions.keys()) + ') VALUES (' +
                    ', '.join('?'* len(self.column_definitions)) + ')'
                ),
                (
                    *self.from_job(job),
                ),
            )
            
    def __delitem__(self, session_or_job: SessionArgs | Job) -> None:
        """Remove a job from the queue."""
        session = get_session(session_or_job)
        with self.cursor() as c:
            c.execute(
                f'DELETE FROM {self.table_name} WHERE session = ?',
                (session.folder,),
            )
            
    def __contains__(self, session_or_job: SessionArgs | Job) -> bool:
        """Whether the session or job is in the queue."""
        session = get_session(session_or_job)
        with self.cursor() as c:
            hits = c.execute(
                f'SELECT * FROM {self.table_name} WHERE session = ?',
                (session.folder,),
            ).fetchall()
        return bool(hits)
        
    def __len__(self) -> int:
        """Number of jobs in the queue."""
        with self.cursor() as c:
            return c.execute(
                f'SELECT count(*) FROM {self.table_name}',
                (),
            ).fetchall()[0][0]
    
    def __iter__(self) -> Iterator[JobT]:
        """Iterate over the jobs in the queue.   
        Sorted by priority (desc), then date added (asc).
        """
        with self.cursor() as c:
            hits = c.execute(
                f'SELECT * FROM {self.table_name} ORDER BY priority DESC, added ASC',
                (),
            ).fetchall()
        return iter(self.to_job(*hit) for hit in hits)
    
    def add_or_update(self, session_or_job: SessionArgs | Job, **kwargs: JobArgs) -> None:
        """Add an entry to the queue or update the existing entry.
        - any kwargs provided will be updated on the job
        - job will be re-queued
        """
        self.update(session_or_job, **kwargs)
        self.set_queued(session_or_job)
        
    def update(self, session_or_job: SessionArgs | Job, **kwargs: JobArgs) -> None:
        """Update an existing entry in the queue.
        Any kwargs provided will be updated on the job.
        """
        job = self.setdefault(session_or_job, get_job(session_or_job, self.job_type)) 
        for key, value in kwargs.items():
            setattr(job, key, value)
        super().update({session_or_job: job})
    
        
    def next(self) -> JobT | None:
        """
        Get the next job to process.
        Sorted by priority (desc), then date added (asc).
        """
        for job in self:
            if not self.is_started(job):
                return job
    
    def set_finished(self, session_or_job: SessionArgs | Job) -> None:
        """Mark a job as finished. May be irreversible, so be sure."""
        self.update(session_or_job, finished=1)
        
    def set_started(self, session_or_job: SessionArgs | Job) -> None:
        """Mark a job as being processed. Reversible"""
        self.update(session_or_job, started=time.time(), hostname=np_config.HOSTNAME, finished=0)
        
    def set_queued(self, session_or_job: SessionArgs | Job) -> None:
        """Mark a job as requiring processing, undoing `set_started`."""
        self.update(session_or_job, started=None, hostname=None, finished=None, errored=None)
    
    def set_errored(self, session_or_job: SessionArgs | Job, error: str | Exception) -> None:
        self.update(session_or_job, error=str(error))
    
    def is_started(self, session_or_job: SessionArgs | Job) -> bool:
        """Whether the job has started processing, but not yet finished."""
        return (
            self[session_or_job].started
            and not self[session_or_job].finished
            and not self[session_or_job].error
        )



if __name__ == '__main__':
    import doctest

    doctest.testmod(verbose=False, raise_on_error=False)
