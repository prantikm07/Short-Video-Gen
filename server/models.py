from pydantic import BaseModel, Field
from typing import List, Optional

class TranscriptSegment(BaseModel):
    text: str
    start: float
    duration: float

    @property
    def end(self) -> float:
        return self.start + self.duration

class ViralMoment(BaseModel):
    title: str = Field(description="Hooking, click-worthy title for this clip (the headline that makes people stop scrolling)")
    caption: str = Field(description="Ready-to-post social media caption for this clip (1-2 punchy lines + 4-6 hashtags)")
    timeline: str = Field(description="Human readable time range of the clip, e.g. '03:25 - 04:10'")
    start_time: float = Field(description="Start time in seconds")
    end_time: float = Field(description="End time in seconds")
    duration: float = Field(description="Total duration in seconds")
    viral_score: int = Field(default=0, description="Viral potential score from 1 to 100")
    reason: str = Field(default="", description="Why this specific clip will hold retention and go viral")
    key_quote: str = Field(default="", description="The most memorable or controversial quote in the clip")

class AnalysisResponse(BaseModel):
    video_summary: str = Field(default="", description="Brief summary of the video content")
    viral_moments: List[ViralMoment] = Field(description="List of selected viral segments ranked by viral score")
