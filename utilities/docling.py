from google import genai
from PIL import Image
from dotenv import load_dotenv
import os

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Initialize the Gemini Client with your API Key
client = genai.Client(api_key="GEMINI_API_KEY")

def describe_image_gemini(image: Image.Image, element_type: str) -> str:
    """Sends PIL Image to Gemini 2.5 Flash API for instant OCR or table conversion."""
    
    # Simple prompt logic depending on image element
    if element_type == "table":
        prompt = "Convert this table image into a clean Markdown table."
    else:
        prompt = "You are Vision Master, you have to extract the vision of the image or chart and create the description of what's going in the image or chart"

    try:
        # Generate content instantly via cloud
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[prompt, image]
        )
        return response.text.strip()
        
    except Exception as e:
        print(f"⚠️ Gemini API Error: {e}")
        
        return f"[{element_type.upper()} EXTRACTION FAILED]"


# Example Usage:
if __name__ == "__main__":
    img = Image.open("sample_table.png")
    result = describe_image_gemini(img, "table")
    print(result)