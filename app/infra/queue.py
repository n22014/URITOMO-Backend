"""
Queue infrastructure configuration

Background job queue management without RQ dependency.
"""

from typing import Any, Dict, Optional
from datetime import datetime

from app.core.logging import get_logger

logger = get_logger(__name__)


class JobQueue:
    """Queue manager for background jobs"""
    
    def __init__(self, queue_name: str = "default"):
        self.queue_name = queue_name
        self._jobs: Dict[str, Dict[str, Any]] = {}
        logger.info(f"JobQueue initialized: {queue_name}")

    def enqueue(
        self,
        func: str,  # "module.path.to.func"
        args: Optional[tuple] = None,
        kwargs: Optional[Dict[str, Any]] = None,
        timeout: int = 300,
        result_ttl: int = 500,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Enqueue a job for processing"""
        try:
            if job_id is None:
                job_id = f"{self.queue_name}_{datetime.now().timestamp()}"
            
            job = {
                "id": job_id,
                "func": func,
                "args": args or (),
                "kwargs": kwargs or {},
                "timeout": timeout,
                "result_ttl": result_ttl,
                "status": "queued",
                "enqueued_at": datetime.now().isoformat(),
            }
            
            self._jobs[job_id] = job
            
            logger.info(
                f"Job enqueued: {job_id}",
                function=func,
                queue=self.queue_name,
            )
            return job
        except Exception as e:
            logger.error(f"Failed to enqueue job: {e}")
            raise

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Get job by ID"""
        return self._jobs.get(job_id)

    def get_all_jobs(self) -> Dict[str, Dict[str, Any]]:
        """Get all jobs in the queue"""
        return self._jobs.copy()

    def remove_job(self, job_id: str) -> bool:
        """Remove a job from the queue"""
        if job_id in self._jobs:
            del self._jobs[job_id]
            logger.info(f"Job removed: {job_id}")
            return True
        return False


class QueueFactory:
    """Factory to get queue instances"""
    
    _queues: Dict[str, JobQueue] = {}
    
    @classmethod
    def get_queue(cls, name: str = "default") -> JobQueue:
        """Get or create a queue instance"""
        if name not in cls._queues:
            cls._queues[name] = JobQueue(name)
        return cls._queues[name]
