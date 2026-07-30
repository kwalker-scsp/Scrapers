### 1. Crawl Part first html -> .md ###
# We used Crawl4AI: Blazing fast async engine that strips the HTML/doc noise, 
# bypasses JS hydration, and delivers a pristine, token-optimized Markdown/JSON stream 
# directly to the model's context window. Pure high-density signal, zero bloat.

### 2. .md -> Gemini Flash 2.5 this is the agentic layer ###
# Embed a model with google to flag dates and events to upkeep the timeline
# This will attach a date and event to add to the timeline
# There's two parts to this it'll flag a) daily events and b) monthly events
# the two parts will be different 'timelines' if you will, as one is comprehensive and the other one flags major things across the month so better for high-level things
# monthly should also allow for any modeling & simulation aspects to cut out some of the daily noise and focus on the bigger picture

### ONNX hugging face model -> this is also an agentic layer but super mini model ###
# https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2 
# using a mini downloadable model we'll run a quick semantic cosine similarity vector
# This scoring cuts out easy duplicate events so you don't have to check for them
# Will still flag lower confidence items, pretty quick check though