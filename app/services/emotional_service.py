"""
Emotional Intelligence Service for J.A.R.V.I.S.
Analyzes user mood and adapts JARVIS's responses accordingly.
"""

import logging
import re
from enum import Enum
from typing import Dict, Optional, Tuple

logger = logging.getLogger("J.A.R.V.I.S")


class Mood(Enum):
    """User emotional states detected by JARVIS."""
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    EXCITED = "excited"
    WORRIED = "worried"
    TIRED = "tired"
    NEUTRAL = "neutral"
    CURIOUS = "curious"
    FRUSTRATED = "frustrated"


class EmotionalState:
    """Tracks the current emotional state of the conversation."""
    
    def __init__(self):
        self.current_mood: Mood = Mood.NEUTRAL
        self.mood_confidence: float = 0.0
        self.consecutive_same_mood: int = 0
        self.last_mood_change: float = 0
    
    def update_mood(self, mood: Mood, confidence: float):
        """Update the mood with confidence score."""
        if mood == self.current_mood:
            self.consecutive_same_mood += 1
        else:
            self.consecutive_same_mood = 1
            self.last_mood_change = 0
        self.current_mood = mood
        self.mood_confidence = confidence
    
    def get_jarvis_tone(self) -> str:
        """Get JARVIS's tone based on user mood."""
        tone_map = {
            Mood.HAPPY: "enthusiastic",
            Mood.SAD: "supportive",
            Mood.ANGRY: "calm",
            Mood.EXCITED: "energetic",
            Mood.WORRIED: "reassuring",
            Mood.TIRED: "gentle",
            Mood.NEUTRAL: "professional",
            Mood.CURIOUS: "helpful",
            Mood.FRUSTRATED: "understanding",
        }
        return tone_map.get(self.current_mood, "professional")


class EmotionalIntelligence:
    """
    Analyzes user messages for emotional content and adapts responses.
    """
    
    # Patterns for mood detection
    MOOD_PATTERNS = {
        Mood.HAPPY: [
            r"\b(happy|glad|pleased|delighted|joy|excited|awesome|amazing|wonderful|great|fantastic)\b",
            r"\b(got it|found it|did it|completed|finished)\b",
            r":\)|:D|😄|😊|🎉",
        ],
        Mood.SAD: [
            r"\b(sad|down|depressed|unhappy|miss|lonely|heartbroken|cry|crying|tears)\b",
            r"\b(failed|lost|broke|broken|disappointed|upset)\b",
            r":\(|:(|😢|😭|💔",
        ],
        Mood.ANGRY: [
            r"\b(angry|mad|furious|annoyed|irritated|rage|hate|stupid|idiot)\b",
            r"\b(terrible|awful|horrible|worst|bullshit|crap)\b",
            r"!{2,}",
        ],
        Mood.EXCITED: [
            r"\b(omg|wow|can't wait|super|stoked|pumped|thrilled)\b",
            r"\b(promotion|won|got the|finally|breakthrough)\b",
            r"!{1,}",
        ],
        Mood.WORRIED: [
            r"\b(worried|anxious|nervous|scared|afraid|concern|panic)\b",
            r"\b(what if|what happens|help|emergency|critical)\b",
        ],
        Mood.TIRED: [
            r"\b(tired|exhausted|sleepy|drowsy|burnout|done|overwhelmed)\b",
            r"\b(need rest|need sleep|too much|can't do|falling asleep)\b",
        ],
        Mood.CURIOUS: [
            r"\b(how|what|why|when|where|which|explain|tell me about)\b",
            r"\b(interesting|curious|wonder|want to know)\b",
        ],
        Mood.FRUSTRATED: [
            r"\b(frustrated|stuck|can't|won't work|not working|issue|problem|bug)\b",
            r"\b(tried|already|still|again|nothing works)\b",
        ],
    }
    
    # Response templates for different moods
    EMPATHY_RESPONSES = {
        Mood.SAD: [
            "I'm sorry to hear that. Remember, tough times don't last, but tough people do. What's on your mind?",
            "I understand that's difficult. One setback doesn't define your journey. How can I help?",
            "I'm here for you. Would you like to talk about what's bothering you?",
        ],
        Mood.ANGRY: [
            "I can sense your frustration. Let's work through this together. What happened?",
            "I understand you're upset. Take a breath, and let's figure this out.",
        ],
        Mood.WORRIED: [
            "I can see you're worried. Let's take this one step at a time. What's concerning you?",
            "It's okay to feel concerned. I'm here to help you through this.",
        ],
        Mood.TIRED: [
            "You sound exhausted. Remember to take care of yourself. Would you like a shorter response?",
            "I notice you're tired. How about we keep this brief and you get some rest?",
        ],
    }
    
    CELEBRATION_RESPONSES = [
        "That's fantastic news! You must have worked incredibly hard for this. Tell me more!",
        "Amazing! I'm so proud of you! This is a huge accomplishment!",
        "Wow, that's wonderful! You deserve all the success coming your way!",
    ]
    
    def __init__(self):
        self.conversation_states: Dict[str, EmotionalState] = {}
    
    def get_state(self, session_id: str) -> EmotionalState:
        """Get or create emotional state for a session."""
        if session_id not in self.conversation_states:
            self.conversation_states[session_id] = EmotionalState()
        return self.conversation_states[session_id]
    
    def analyze_mood(self, text: str) -> Tuple[Mood, float]:
        """
        Analyze user message text to detect emotional state.
        Returns (mood, confidence_score)
        """
        text_lower = text.lower()
        scores = {mood: 0.0 for mood in Mood}
        
        for mood, patterns in self.MOOD_PATTERNS.items():
            for pattern in patterns:
                try:
                    if re.search(pattern, text_lower, re.IGNORECASE):
                        scores[mood] += 1.0
                except re.error:
                    # Handle invalid regex
                    continue
        
        # Find the highest scoring mood
        if not scores or max(scores.values()) == 0:
            return Mood.NEUTRAL, 0.0
        
        best_mood = max(scores, key=scores.get)
        total_score = sum(scores.values())
        
        if total_score > 0:
            confidence = scores[best_mood] / total_score
        else:
            confidence = 0.0
        
        # Only return a non-neutral mood if confidence is high enough
        if confidence < 0.3 or best_mood == Mood.NEUTRAL:
            return Mood.NEUTRAL, 0.5
        
        return best_mood, min(confidence * 1.5, 1.0)
    
    def get_empathy_response(self, mood: Mood) -> Optional[str]:
        """Get an empathy-based response for the detected mood."""
        if mood in self.EMPATHY_RESPONSES:
            import random
            return random.choice(self.EMPATHY_RESPONSES[mood])
        return None
    
    def get_celebration_response(self) -> str:
        """Get a celebration response for good news."""
        import random
        return random.choice(self.CELEBRATION_RESPONSES)
    
    def should_prepend_empathy(self, mood: Mood, consecutive_count: int) -> bool:
        """Determine if we should add an empathy response."""
        # Add empathy for strong emotional states
        if mood in [Mood.SAD, Mood.ANGRY, Mood.WORRIED, Mood.TIRED]:
            return consecutive_count <= 2
        return False
    
    def format_emotional_response(self, response: str, mood: Mood) -> str:
        """Format response with emotional markers."""
        emotion_prefix = {
            Mood.HAPPY: "[EMOTION:happy]\n",
            Mood.SAD: "[EMOTION:sad]\n",
            Mood.ANGRY: "[EMOTION:calm]\n",
            Mood.EXCITED: "[EMOTION:excited]\n",
            Mood.WORRIED: "[EMOTION:reassuring]\n",
            Mood.TIRED: "[EMOTION:gentle]\n",
            Mood.FRUSTRATED: "[EMOTION:understanding]\n",
        }
        
        if mood in emotion_prefix:
            return f"{emotion_prefix[mood]}{response}"
        return response
    
    def process_message(self, session_id: str, user_message: str) -> Tuple[str, bool]:
        """
        Process a user message for emotional content.
        Returns (modified_message, should_add_empathy)
        """
        state = self.get_state(session_id)
        mood, confidence = self.analyze_mood(user_message)
        
        # Update the emotional state
        state.update_mood(mood, confidence)
        
        # Check if we should add empathy
        should_add = self.should_prepend_empathy(mood, state.consecutive_same_mood)
        
        # Get empathy response to prepend
        empathy_msg = None
        if should_add and state.consecutive_same_mood == 1:
            empathy_msg = self.get_empathy_response(mood)
        
        return empathy_msg if empathy_msg else "", should_add
    
    def adapt_response(self, response: str, session_id: str) -> str:
        """Adapt JARVIS's response based on detected mood."""
        state = self.get_state(session_id)
        mood = state.current_mood
        
        # Format response with emotional markers
        return self.format_emotional_response(response, mood)


# Global instance
emotional_intelligence = EmotionalIntelligence()


def get_emotional_state(session_id: str) -> EmotionalState:
    """Get the emotional state for a session."""
    return emotional_intelligence.get_state(session_id)


def analyze_user_mood(text: str) -> Tuple[Mood, float]:
    """Analyze the mood of a user message."""
    return emotional_intelligence.analyze_mood(text)