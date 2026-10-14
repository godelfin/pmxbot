def style(visual_definition, signature_elements, avoid, compatible_styles):
    return {
        "visual_definition": visual_definition,
        "signature_elements": signature_elements,
        "avoid": avoid,
        "compatible_styles": compatible_styles,
    }


ALBUM_ART_STYLES = {
    # ============================================================
    # 1. MINIMALISM & ABSTRACT DESIGN
    # ============================================================
    "001": {
        "name": "Minimalism",
        **style(
            "Reduced visual composition emphasizing simplicity, clarity, and negative space.",
            ["Limited palette", "Sparse imagery", "Negative space", "Clean forms"],
            [
                "Clutter",
                "Excessive ornamentation",
                "Busy backgrounds",
                "Competing focal points",
            ],
            ["Geometric Minimalism", "Typographic Minimalism", "Color Field"],
        ),
    },
    "002": {
        "name": "Geometric Minimalism",
        **style(
            "Minimal compositions constructed from precise geometric shapes and relationships.",
            [
                "Circles",
                "Triangles",
                "Straight lines",
                "Flat colors",
                "Precise alignment",
            ],
            ["Organic clutter", "Complex textures", "Decorative excess"],
            ["Minimalism", "Bauhaus", "Hard-Edge Abstraction"],
        ),
    },
    "003": {
        "name": "Abstract Minimalism",
        **style(
            "Nonrepresentational imagery reduced to a small number of expressive visual forms.",
            [
                "Abstract shapes",
                "Subtle tonal variation",
                "Negative space",
                "Visual restraint",
            ],
            ["Literal illustration", "Detailed realism", "Dense compositions"],
            ["Minimalism", "Color Field", "Organic Abstraction"],
        ),
    },
    "004": {
        "name": "Typographic Minimalism",
        **style(
            "Typography serves as the dominant visual element within a deliberately restrained composition.",
            [
                "Simple lettering",
                "Limited colors",
                "Whitespace",
                "Strong typographic hierarchy",
            ],
            [
                "Decorative fonts",
                "Complex illustrations",
                "Excessive text",
                "Ornamentation",
            ],
            ["Minimalism", "Swiss / International Style", "Anti-Design"],
        ),
    },
    "005": {
        "name": "Swiss / International Style",
        **style(
            "Rational graphic design emphasizing readability, systematic composition, and visual order.",
            ["Modular grids", "Sans-serif typography", "Asymmetry", "Clear hierarchy"],
            ["Decorative clutter", "Irregular spacing", "Unnecessary embellishment"],
            ["Minimalism", "Geometric Minimalism", "Bauhaus"],
        ),
    },
    "006": {
        "name": "Bauhaus",
        **style(
            "Functional modernist design combining basic geometric forms with bold graphic relationships.",
            [
                "Primary colors",
                "Geometric shapes",
                "Constructed layouts",
                "Functional typography",
            ],
            ["Victorian ornament", "Photorealistic decoration", "Excessive complexity"],
            ["Geometric Minimalism", "Constructivism", "Swiss / International Style"],
        ),
    },
    "007": {
        "name": "Color Field",
        **style(
            "Large uninterrupted areas of color create an immersive and emotionally expressive composition.",
            [
                "Expansive color areas",
                "Subtle boundaries",
                "Restricted palette",
                "Minimal imagery",
            ],
            ["Detailed illustration", "Busy patterns", "Strong figurative narratives"],
            ["Minimalism", "Abstract Minimalism", "Hard-Edge Abstraction"],
        ),
    },
    "008": {
        "name": "Hard-Edge Abstraction",
        **style(
            "Precise abstract compositions defined by sharply separated areas of flat color.",
            [
                "Crisp contours",
                "Flat colors",
                "Geometric divisions",
                "Clean intersections",
            ],
            ["Soft gradients", "Painterly blending", "Photographic textures"],
            ["Geometric Minimalism", "Color Field", "Op Art"],
        ),
    },
    "009": {
        "name": "Op Art",
        **style(
            "Geometric patterns create optical illusions of movement, vibration, and spatial distortion.",
            [
                "Repeating lines",
                "High contrast",
                "Geometric repetition",
                "Optical interference",
            ],
            ["Random decoration", "Soft painterly forms", "Unstructured layouts"],
            ["Geometric Minimalism", "Hard-Edge Abstraction", "Psychedelic Art"],
        ),
    },
    "010": {
        "name": "Scientific / Data Visualization",
        **style(
            "Scientific information and mathematical structures transformed into compelling graphic imagery.",
            [
                "Waveforms",
                "Plotted lines",
                "Graphs",
                "Scientific diagrams",
                "Coordinate systems",
            ],
            [
                "Decorative randomness",
                "Unstructured illustration",
                "Excessive ornamentation",
            ],
            ["Minimalism", "Generative / Algorithmic Art", "Geometric Minimalism"],
        ),
    },
    # ============================================================
    # 2. SURREALISM, FANTASY & PSYCHEDELIA
    # ============================================================
    "011": {
        "name": "Surrealism",
        **style(
            "Dreamlike compositions combining familiar objects in impossible or irrational relationships.",
            [
                "Unexpected juxtapositions",
                "Impossible objects",
                "Dream logic",
                "Symbolic imagery",
            ],
            ["Conventional realism", "Predictable scenes", "Literal storytelling"],
            ["Magical Realism", "Metaphysical Art", "Cosmic Surrealism"],
        ),
    },
    "012": {
        "name": "Dreamcore",
        **style(
            "Nostalgic and uncanny environments resembling fragmented memories or ambiguous dreams.",
            ["Empty spaces", "Soft haze", "Uncanny familiarity", "Dreamlike lighting"],
            [
                "Sharp commercial polish",
                "Logical environments",
                "Overly explicit narratives",
            ],
            ["Liminal Spaces", "Ethereal / Dreamy", "Surrealism"],
        ),
    },
    "013": {
        "name": "Psychedelic Art",
        **style(
            "Highly expressive imagery suggesting altered perception, sensory intensity, and visual hallucination.",
            ["Vibrant colors", "Warped shapes", "Intricate patterns", "Fluid geometry"],
            [
                "Muted corporate layouts",
                "Strict realism",
                "Conventional visual restraint",
            ],
            ["Visionary Art", "Op Art", "Cosmic Surrealism"],
        ),
    },
    "014": {
        "name": "Visionary Art",
        **style(
            "Intricate symbolic imagery exploring consciousness, spirituality, and transcendent experiences.",
            [
                "Sacred patterns",
                "Luminous forms",
                "Complex symmetry",
                "Spiritual symbolism",
            ],
            [
                "Mundane realism",
                "Flat corporate graphics",
                "Emotionally neutral imagery",
            ],
            ["Psychedelic Art", "Sacred Geometry", "Mysticism / Occult Art"],
        ),
    },
    "015": {
        "name": "Magical Realism",
        **style(
            "Believable environments containing subtle, extraordinary elements presented as natural occurrences.",
            [
                "Realistic settings",
                "Subtle impossibilities",
                "Poetic symbolism",
                "Natural lighting",
            ],
            [
                "Extreme abstraction",
                "Overt fantasy spectacle",
                "Chaotic visual effects",
            ],
            ["Surrealism", "Nature Romanticism", "Cinematic Photography"],
        ),
    },
    "016": {
        "name": "Dark Fantasy",
        **style(
            "Fantastical worlds dominated by ominous atmosphere, supernatural imagery, and Gothic themes.",
            ["Ruins", "Mythical creatures", "Deep shadows", "Ancient architecture"],
            ["Playful cartoon colors", "Cheerful minimalism", "Everyday realism"],
            ["Gothic / Goth", "Horror / Macabre", "High Fantasy"],
        ),
    },
    "017": {
        "name": "High Fantasy",
        **style(
            "Grand imaginative worlds featuring magical environments, mythology, and epic visual storytelling.",
            [
                "Mythical landscapes",
                "Monumental structures",
                "Fantasy creatures",
                "Dramatic skies",
            ],
            ["Urban mundanity", "Minimal imagery", "Modern corporate design"],
            ["Dark Fantasy", "Nature Romanticism", "Visionary Art"],
        ),
    },
    "018": {
        "name": "Cosmic Surrealism",
        **style(
            "Impossible dream imagery set within vast astronomical and interstellar environments.",
            [
                "Celestial objects",
                "Impossible planets",
                "Cosmic landscapes",
                "Scale distortion",
            ],
            [
                "Ordinary terrestrial scenes",
                "Rigid realism",
                "Conventional compositions",
            ],
            ["Surrealism", "Celestial Art", "Psychedelic Art"],
        ),
    },
    "019": {
        "name": "Symbolism",
        **style(
            "Visual expression built around metaphorical objects, allegorical figures, and hidden meanings.",
            [
                "Archetypal objects",
                "Mystical figures",
                "Allegorical compositions",
                "Visual metaphors",
            ],
            ["Literal explanation", "Random imagery", "Purely decorative elements"],
            ["Surrealism", "Dark Romanticism", "Mysticism / Occult Art"],
        ),
    },
    "020": {
        "name": "Metaphysical Art",
        **style(
            "Enigmatic spaces and objects arranged to create philosophical mystery and uncanny stillness.",
            [
                "Empty plazas",
                "Long shadows",
                "Classical architecture",
                "Strange object placement",
            ],
            ["Crowded activity", "Busy storytelling", "Chaotic movement"],
            ["Surrealism", "Liminal Spaces", "Magical Realism"],
        ),
    },
    # ============================================================
    # 3. RETRO, VINTAGE & NOSTALGIA
    # ============================================================
    "021": {
        "name": "1950s Retro",
        **style(
            "Optimistic mid-century commercial design inspired by postwar advertising and popular culture.",
            [
                "Vintage advertisements",
                "Playful lettering",
                "Pastel colors",
                "Retro illustration",
            ],
            ["Digital glitches", "Cyberpunk imagery", "Contemporary UI graphics"],
            ["Mid-Century Modern", "Pop Art", "Retro Comic / Pulp"],
        ),
    },
    "022": {
        "name": "1960s Pop",
        **style(
            "Bold visual culture combining youthful optimism, graphic experimentation, and pop-era aesthetics.",
            ["Bright colors", "Graphic patterns", "Bold lettering", "Mod fashion"],
            ["Minimal corporate neutrality", "Dark industrial realism"],
            ["Pop Art", "Psychedelic Art", "Art Nouveau"],
        ),
    },
    "023": {
        "name": "1970s Vintage",
        **style(
            "Warm nostalgic design inspired by 1970s popular culture, photography, and printed graphics.",
            ["Earth tones", "Rounded typography", "Warm grain", "Retro ornamentation"],
            ["Glossy CGI", "Cold cyber aesthetics", "Excessive digital sharpness"],
            ["Analog Nostalgia", "Psychedelic Art", "Lo-Fi Indie"],
        ),
    },
    "024": {
        "name": "1980s Retro",
        **style(
            "Bold synthetic visual identity inspired by 1980s pop culture and early digital graphics.",
            ["Neon accents", "Chrome text", "Strong gradients", "Retro geometry"],
            [
                "Rustic handmade textures",
                "Victorian ornament",
                "Naturalistic restraint",
            ],
            ["Synthwave / Retrowave", "Vaporwave", "Glamour Photography"],
        ),
    },
    "025": {
        "name": "1990s Nostalgia",
        **style(
            "Alternative-era graphics mixing analog imperfection, early digital culture, and youth subcultures.",
            [
                "Distressed typography",
                "Grainy images",
                "Retro computer graphics",
                "Collage",
            ],
            [
                "Ultra-clean corporate layouts",
                "Luxury minimalism",
                "Futuristic CGI polish",
            ],
            ["Grunge", "Lo-Fi Indie", "Internet-Core"],
        ),
    },
    "026": {
        "name": "Y2K",
        **style(
            "Turn-of-the-millennium futurism characterized by glossy surfaces and playful technological optimism.",
            [
                "Chrome",
                "Translucent plastics",
                "Metallic lettering",
                "Iridescent gradients",
            ],
            ["Antique textures", "Rustic decoration", "Muted historical palettes"],
            ["Cyber Y2K", "Hyperreal CGI", "Vaporwave"],
        ),
    },
    "027": {
        "name": "Mid-Century Modern",
        **style(
            "Refined modernist design using simplified organic and geometric forms with understated elegance.",
            [
                "Organic shapes",
                "Muted retro colors",
                "Simple illustration",
                "Balanced asymmetry",
            ],
            ["Gothic ornament", "Chaotic layouts", "Excessive gradients"],
            ["1950s Retro", "Bauhaus", "Geometric Minimalism"],
        ),
    },
    "028": {
        "name": "Art Deco",
        **style(
            "Elegant decorative design emphasizing geometric luxury, symmetry, and monumental sophistication.",
            ["Gold accents", "Sunburst patterns", "Stepped geometry", "Symmetry"],
            ["Casual handwriting", "Grunge textures", "Irregular DIY layouts"],
            ["Luxury Editorial", "Geometric Minimalism", "Glamour Photography"],
        ),
    },
    "029": {
        "name": "Art Nouveau",
        **style(
            "Decorative organic design characterized by flowing lines and stylized natural forms.",
            [
                "Curved linework",
                "Floral ornament",
                "Elegant figures",
                "Botanical motifs",
            ],
            ["Harsh industrial grids", "Angular brutalism", "Minimal ornamentation"],
            ["Botanical Art", "Psychedelic Art", "Symbolism"],
        ),
    },
    "030": {
        "name": "Victorian / Antique",
        **style(
            "Historically inspired visual design featuring ornate printmaking, decorative typography, and archival imagery.",
            [
                "Engraved illustrations",
                "Decorative borders",
                "Antique lettering",
                "Aged paper",
            ],
            ["Neon gradients", "Modern UI graphics", "Chrome effects"],
            ["Dark Academia", "Gothic / Goth", "Retro Comic / Pulp"],
        ),
    },
    # ============================================================
    # 4. PUNK, ALTERNATIVE & UNDERGROUND
    # ============================================================
    "031": {
        "name": "Punk / DIY",
        **style(
            "Confrontational, deliberately unpolished graphic design expressing rebellion and independence.",
            [
                "Ransom-note lettering",
                "Torn paper",
                "Harsh contrast",
                "Irregular composition",
            ],
            ["Luxury polish", "Perfect symmetry", "Corporate design conventions"],
            ["Riot Grrrl / Zine", "Grunge", "Anti-Design"],
        ),
    },
    "032": {
        "name": "Grunge",
        **style(
            "Rough alternative aesthetic emphasizing wear, decay, imperfection, and distressed graphic surfaces.",
            ["Scratches", "Distressed text", "Dirty textures", "Muted colors"],
            ["Pristine surfaces", "Perfect geometry", "Glossy advertising"],
            ["Punk / DIY", "1990s Nostalgia", "Lo-Fi Indie"],
        ),
    },
    "033": {
        "name": "Post-Punk",
        **style(
            "Intellectual and emotionally detached visual language combining minimalism, industrial references, and abstraction.",
            [
                "Monochrome graphics",
                "Sparse imagery",
                "Experimental typography",
                "Abstract symbols",
            ],
            [
                "Cheerful ornamentation",
                "Bright commercial illustration",
                "Visual excess",
            ],
            ["Minimalism", "Industrial", "Scientific / Data Visualization"],
        ),
    },
    "034": {
        "name": "Gothic / Goth",
        **style(
            "Dark romantic visual identity inspired by Gothic architecture, melancholic symbolism, and shadowy atmospheres.",
            ["Black palettes", "Gothic lettering", "Ruins", "Dark silhouettes"],
            [
                "Bright playful palettes",
                "Optimistic advertising",
                "Casual cartoon graphics",
            ],
            ["Dark Romanticism", "Dark Fantasy", "Victorian / Antique"],
        ),
    },
    "035": {
        "name": "Industrial",
        **style(
            "Cold mechanical aesthetic inspired by factories, machinery, urban infrastructure, and technological alienation.",
            [
                "Metal surfaces",
                "Mechanical forms",
                "Concrete textures",
                "Functional typography",
            ],
            ["Floral ornament", "Soft pastoral colors", "Decorative romanticism"],
            ["Post-Punk", "Dystopian", "Brutalist Graphic Design"],
        ),
    },
    "036": {
        "name": "Riot Grrrl / Zine",
        **style(
            "Handmade-looking activist graphics influenced by independent publishing, feminist punk culture, and photocopied zines.",
            ["Photocopy textures", "Cutout text", "Handwriting", "Raw collage"],
            ["Commercial perfection", "Luxury design", "Excessively polished layouts"],
            ["Punk / DIY", "Dada Collage", "Grunge"],
        ),
    },
    "037": {
        "name": "Lo-Fi Indie",
        **style(
            "Intimate understated visual identity emphasizing authenticity, nostalgia, and deliberate imperfection.",
            [
                "Faded photography",
                "Simple typography",
                "Muted colors",
                "Handmade appearance",
            ],
            ["Overproduced CGI", "Aggressive neon", "Luxury styling"],
            ["Analog Nostalgia", "1990s Nostalgia", "Intimate Portraiture"],
        ),
    },
    "038": {
        "name": "Noise / Experimental",
        **style(
            "Visually disruptive art emphasizing chaos, irregularity, distortion, and unconventional structures.",
            [
                "Dense textures",
                "Visual interference",
                "Fragmentation",
                "Aggressive contrast",
            ],
            ["Conventional harmony", "Commercial clarity", "Predictable composition"],
            ["Glitch Art", "Abstract Expressionism", "Deconstructivism"],
        ),
    },
    "039": {
        "name": "Hardcore / Metal",
        **style(
            "Intense aggressive artwork emphasizing power, confrontation, extreme emotion, and dramatic imagery.",
            [
                "Dark palettes",
                "Aggressive lettering",
                "Menacing imagery",
                "High contrast",
            ],
            ["Playful pastels", "Corporate simplicity", "Lighthearted illustration"],
            ["Dark Fantasy", "Horror / Macabre", "Industrial"],
        ),
    },
    "040": {
        "name": "Underground Street Art",
        **style(
            "Raw urban graphic expression inspired by graffiti, murals, tagging, and unauthorized public art.",
            [
                "Spray-paint marks",
                "Stencil graphics",
                "Urban textures",
                "Bold lettering",
            ],
            ["Luxury polish", "Perfect alignment", "Sterile photographic backgrounds"],
            ["Punk / DIY", "Contemporary Streetwear", "Neo-Pop"],
        ),
    },
    # ============================================================
    # 5. POP ART, ILLUSTRATION & GRAPHIC ART
    # ============================================================
    "041": {
        "name": "Pop Art",
        **style(
            "Bold graphic imagery inspired by advertising, consumer culture, celebrity, and mass-produced media.",
            [
                "Flat vivid colors",
                "Commercial symbols",
                "Bold outlines",
                "Repeated imagery",
            ],
            [
                "Subtle naturalism",
                "Muted romantic landscapes",
                "Excessive painterly ambiguity",
            ],
            ["Neo-Pop", "1960s Pop", "Comic Book Art"],
        ),
    },
    "042": {
        "name": "Neo-Pop",
        **style(
            "Contemporary reinterpretation of Pop Art combining popular culture, playful imagery, and vivid graphic identities.",
            [
                "Bright colors",
                "Pop-culture symbols",
                "Stylized characters",
                "Bold compositions",
            ],
            ["Subdued historical realism", "Minimal emotional expression"],
            ["Pop Art", "Lowbrow / Pop Surrealism", "Kawaii"],
        ),
    },
    "043": {
        "name": "Comic Book Art",
        **style(
            "Graphic storytelling aesthetic inspired by illustrated comics and sequential visual narratives.",
            [
                "Bold ink outlines",
                "Dramatic poses",
                "Halftone shading",
                "Action framing",
            ],
            ["Soft photographic realism", "Minimal geometric abstraction"],
            ["Retro Comic / Pulp", "Graphic Novel", "Pop Art"],
        ),
    },
    "044": {
        "name": "Manga / Anime",
        **style(
            "Stylized Japanese comic and animation aesthetics emphasizing expressive characters and cinematic compositions.",
            [
                "Expressive faces",
                "Stylized anatomy",
                "Dynamic framing",
                "Illustrative shading",
            ],
            ["Western corporate realism", "Rigid photographic conventions"],
            ["Kawaii", "Cyberpunk", "Graphic Novel"],
        ),
    },
    "045": {
        "name": "Cartoon Art",
        **style(
            "Playful illustration using simplified forms, exaggerated character proportions, and expressive outlines.",
            [
                "Bold outlines",
                "Simplified shapes",
                "Expressive characters",
                "Flat colors",
            ],
            [
                "Strict realism",
                "Muted documentary aesthetics",
                "Complex natural anatomy",
            ],
            ["Comic Book Art", "Neo-Pop", "Naïve / Outsider Art"],
        ),
    },
    "046": {
        "name": "Editorial Illustration",
        **style(
            "Conceptual illustration that communicates an idea through metaphor, symbolism, and concise visual storytelling.",
            [
                "Visual metaphors",
                "Stylized objects",
                "Controlled composition",
                "Symbolic imagery",
            ],
            [
                "Decorative randomness",
                "Unnecessary complexity",
                "Literal photographic representation",
            ],
            ["Conceptual Graphic Art", "Surrealism", "Minimalism"],
        ),
    },
    "047": {
        "name": "Retro Comic / Pulp",
        **style(
            "Dramatic vintage illustration inspired by pulp magazines, classic comics, and sensational printed entertainment.",
            [
                "Halftone dots",
                "Vintage lettering",
                "Dramatic expressions",
                "Aged colors",
            ],
            [
                "Modern glossy gradients",
                "Corporate minimalism",
                "Photorealistic neutrality",
            ],
            ["Comic Book Art", "1950s Retro", "Pop Art"],
        ),
    },
    "048": {
        "name": "Lowbrow / Pop Surrealism",
        **style(
            "Playful but unsettling imagery combining cartoon influences, surreal symbolism, and countercultural humor.",
            [
                "Strange characters",
                "Exaggerated anatomy",
                "Surreal objects",
                "Bright-dark contrast",
            ],
            [
                "Conventional realism",
                "Corporate restraint",
                "Strictly functional graphics",
            ],
            ["Neo-Pop", "Surrealism", "Cartoon Art"],
        ),
    },
    "049": {
        "name": "Naïve / Outsider Art",
        **style(
            "Expressive unconventional artwork characterized by intuitive composition and nonacademic visual representation.",
            [
                "Unusual proportions",
                "Raw mark-making",
                "Simplified forms",
                "Spontaneous composition",
            ],
            ["Academic perfection", "Rigid perspective", "Commercial polish"],
            ["Cartoon Art", "Expressionism", "Lo-Fi Indie"],
        ),
    },
    "050": {
        "name": "Graphic Novel",
        **style(
            "Sophisticated narrative illustration combining cinematic composition with sequential-art visual conventions.",
            [
                "Dramatic shadows",
                "Illustrative realism",
                "Expressive linework",
                "Narrative composition",
            ],
            ["Flat corporate graphics", "Random abstraction", "Decorative minimalism"],
            ["Comic Book Art", "Film Noir", "Manga / Anime"],
        ),
    },
    # ============================================================
    # 6. FINE ART & HISTORICAL MOVEMENTS
    # ============================================================
    "051": {
        "name": "Expressionism",
        **style(
            "Emotion-driven representation emphasizing psychological intensity over realistic accuracy.",
            [
                "Distorted figures",
                "Strong colors",
                "Expressive contours",
                "Emotional tension",
            ],
            ["Photographic neutrality", "Perfect naturalism", "Decorative restraint"],
            ["Abstract Expressionism", "Dark Romanticism", "Naïve / Outsider Art"],
        ),
    },
    "052": {
        "name": "Abstract Expressionism",
        **style(
            "Nonrepresentational artwork communicating emotion through energetic gesture, color, and spontaneous composition.",
            [
                "Gestural marks",
                "Dynamic textures",
                "Expressive color",
                "Irregular forms",
            ],
            ["Precise grids", "Literal subjects", "Mechanical repetition"],
            ["Expressionism", "Color Field", "Noise / Experimental"],
        ),
    },
    "053": {
        "name": "Impressionism",
        **style(
            "Atmospheric representation emphasizing fleeting light, color relationships, and perceptual softness.",
            [
                "Soft edges",
                "Luminous colors",
                "Visible brushwork",
                "Atmospheric scenes",
            ],
            ["Hard geometric edges", "Mechanical perfection", "Harsh graphic outlines"],
            ["Nature Romanticism", "Post-Impressionism", "Ethereal / Dreamy"],
        ),
    },
    "054": {
        "name": "Post-Impressionism",
        **style(
            "Expressive reinterpretation of observed reality using stylized forms, bold colors, and personal artistic structure.",
            [
                "Expressive brushwork",
                "Non-naturalistic colors",
                "Simplified forms",
                "Strong contours",
            ],
            ["Photographic accuracy", "Sterile composition", "Mechanical flatness"],
            ["Impressionism", "Expressionism", "Fauvist-inspired color"],
        ),
    },
    "055": {
        "name": "Cubism",
        **style(
            "Fragmented representation of objects and figures through multiple simultaneous viewpoints.",
            [
                "Faceted shapes",
                "Overlapping planes",
                "Geometric fragmentation",
                "Multiple perspectives",
            ],
            [
                "Single-point realism",
                "Smooth photographic blending",
                "Naturalistic depth",
            ],
            ["Geometric Minimalism", "Deconstructivism", "Constructivism"],
        ),
    },
    "056": {
        "name": "Futurism",
        **style(
            "Dynamic art emphasizing movement, speed, technological progress, and mechanical energy.",
            ["Motion lines", "Repeating forms", "Angular movement", "Machine imagery"],
            ["Static compositions", "Pastoral calm", "Delicate ornamentation"],
            ["Constructivism", "Cyberpunk", "Industrial"],
        ),
    },
    "057": {
        "name": "Dadaism",
        **style(
            "Absurdist anti-conventional art built around chance, disruption, appropriation, and irrational combinations.",
            [
                "Found imagery",
                "Unexpected objects",
                "Irregular typography",
                "Absurd juxtapositions",
            ],
            ["Conventional beauty", "Perfect harmony", "Literal narratives"],
            ["Dada Collage", "Surrealism", "Anti-Design"],
        ),
    },
    "058": {
        "name": "Constructivism",
        **style(
            "Bold geometric graphic language emphasizing dynamic composition and socially oriented visual communication.",
            [
                "Diagonal geometry",
                "Red-black palettes",
                "Bold typography",
                "Photomontage",
            ],
            [
                "Organic romantic ornament",
                "Soft decorative pastels",
                "Passive compositions",
            ],
            ["Bauhaus", "Futurism", "Geometric Minimalism"],
        ),
    },
    "059": {
        "name": "Baroque",
        **style(
            "Grand dramatic art emphasizing theatrical light, emotional intensity, movement, and elaborate visual richness.",
            ["Chiaroscuro", "Dynamic poses", "Rich colors", "Elaborate compositions"],
            ["Flat minimalism", "Casual snapshots", "Understated geometry"],
            ["Dark Romanticism", "Renaissance Classicism", "Glamour Photography"],
        ),
    },
    "060": {
        "name": "Renaissance Classicism",
        **style(
            "Idealized figurative compositions emphasizing harmony, balanced proportions, and classical artistic conventions.",
            [
                "Classical figures",
                "Balanced composition",
                "Architectural perspective",
                "Idealized anatomy",
            ],
            ["Chaotic distortion", "Digital glitch effects", "Random fragmentation"],
            ["Baroque", "Symbolism", "Art Deco"],
        ),
    },
    # ============================================================
    # 7. PHOTOGRAPHY & PORTRAIT AESTHETICS
    # ============================================================
    "061": {
        "name": "Fine Art Photography",
        **style(
            "Carefully composed photographic imagery emphasizing artistic intention, visual symbolism, and aesthetic refinement.",
            [
                "Deliberate framing",
                "Controlled lighting",
                "Strong composition",
                "Visual restraint",
            ],
            ["Random snapshots", "Commercial clutter", "Uncontrolled distractions"],
            ["Conceptual Photography", "Black-and-White Editorial", "Minimalism"],
        ),
    },
    "062": {
        "name": "Black-and-White Editorial",
        **style(
            "Expressive monochromatic photography emphasizing form, contrast, texture, and personality.",
            [
                "Grayscale tones",
                "Strong contrast",
                "Portrait framing",
                "Expressive shadows",
            ],
            ["Saturated colors", "Neon gradients", "Decorative overlays"],
            ["Fine Art Photography", "Film Noir", "Intimate Portraiture"],
        ),
    },
    "063": {
        "name": "Fashion Editorial",
        **style(
            "Highly art-directed photography using fashion, styling, and posing to construct a distinctive identity.",
            [
                "Styled outfits",
                "Controlled lighting",
                "Sophisticated posing",
                "Graphic composition",
            ],
            [
                "Unintentional clutter",
                "Casual snapshots",
                "Poorly defined focal points",
            ],
            ["Luxury Editorial", "Avant-Garde Fashion", "Glamour Photography"],
        ),
    },
    "064": {
        "name": "Cinematic Photography",
        **style(
            "Photography designed to resemble a carefully composed frame from a motion picture.",
            [
                "Narrative lighting",
                "Atmospheric depth",
                "Cinematic framing",
                "Color grading",
            ],
            ["Flat commercial lighting", "Random framing", "Decorative clutter"],
            ["Film Noir", "Neo-Noir", "Conceptual Photography"],
        ),
    },
    "065": {
        "name": "Documentary Realism",
        **style(
            "Authentic observational photography emphasizing real environments and unembellished human experience.",
            [
                "Natural lighting",
                "Candid subjects",
                "Real locations",
                "Unposed gestures",
            ],
            ["Artificial staging", "Surreal manipulation", "Overprocessed effects"],
            ["Street Photography", "Analog Nostalgia", "Intimate Portraiture"],
        ),
    },
    "066": {
        "name": "Street Photography",
        **style(
            "Observational urban imagery capturing everyday life, spontaneous encounters, and environmental character.",
            [
                "Urban settings",
                "Available light",
                "Spontaneous framing",
                "Human activity",
            ],
            [
                "Elaborate studio staging",
                "Artificial fantasy elements",
                "Excessive retouching",
            ],
            ["Documentary Realism", "Analog Nostalgia", "Film Noir"],
        ),
    },
    "067": {
        "name": "Conceptual Photography",
        **style(
            "Photographic scenes deliberately constructed to communicate an idea, metaphor, or symbolic narrative.",
            [
                "Staged imagery",
                "Visual metaphors",
                "Symbolic objects",
                "Controlled composition",
            ],
            ["Unrelated decoration", "Unfocused storytelling", "Random imagery"],
            ["Surrealism", "Fine Art Photography", "Magical Realism"],
        ),
    },
    "068": {
        "name": "Intimate Portraiture",
        **style(
            "Emotionally direct portrait imagery emphasizing vulnerability, identity, and human presence.",
            [
                "Close framing",
                "Natural expressions",
                "Personal atmosphere",
                "Understated styling",
            ],
            [
                "Excessive theatricality",
                "Distracting effects",
                "Overly commercial posing",
            ],
            ["Black-and-White Editorial", "Lo-Fi Indie", "Fine Art Photography"],
        ),
    },
    "069": {
        "name": "Glamour Photography",
        **style(
            "Highly polished portraiture emphasizing beauty, charisma, theatrical styling, and visual sophistication.",
            [
                "Dramatic lighting",
                "Elegant posing",
                "Refined styling",
                "Smooth tonal transitions",
            ],
            [
                "Harsh documentary realism",
                "Unintentional imperfections",
                "Visual disorder",
            ],
            ["Fashion Editorial", "Luxury Editorial", "Art Deco"],
        ),
    },
    "070": {
        "name": "Analog Nostalgia",
        **style(
            "Photographic aesthetic evoking physical film, faded memories, and the imperfections of analog imaging.",
            [
                "Film grain",
                "Soft contrast",
                "Faded colors",
                "Light leaks",
                "Subtle imperfections",
            ],
            ["Digital oversharpening", "Plastic CGI surfaces", "Perfect gradients"],
            ["Lo-Fi Indie", "1970s Vintage", "1990s Nostalgia"],
        ),
    },
    # ============================================================
    # 8. DIGITAL, FUTURISTIC & EXPERIMENTAL
    # ============================================================
    "071": {
        "name": "Glitch Art",
        **style(
            "Digital imagery deliberately disrupted through visual errors, corrupted structures, and electronic distortion.",
            [
                "Pixel displacement",
                "RGB separation",
                "Signal interference",
                "Digital artifacts",
            ],
            ["Pristine photography", "Smooth natural realism", "Conventional polish"],
            ["Noise / Experimental", "Digital Abstraction", "Cyberpunk"],
        ),
    },
    "072": {
        "name": "Pixel Art",
        **style(
            "Stylized digital imagery constructed from visible pixel units and restricted-resolution graphics.",
            [
                "Blocky pixels",
                "Limited palette",
                "Retro game aesthetics",
                "Grid-aligned forms",
            ],
            [
                "Smooth photorealism",
                "Anti-aliased gradients",
                "Organic painterly blending",
            ],
            ["Internet-Core", "Vaporwave", "Cyber Y2K"],
        ),
    },
    "073": {
        "name": "Vaporwave",
        **style(
            "Nostalgic internet-era aesthetic remixing retro commercial culture, classical imagery, and digital melancholy.",
            ["Pastel neon", "Classical busts", "Retro interfaces", "Digital sunsets"],
            ["Strict realism", "Rustic minimalism", "Historical authenticity"],
            ["Synthwave / Retrowave", "Y2K", "Internet-Core"],
        ),
    },
    "074": {
        "name": "Synthwave / Retrowave",
        **style(
            "Retro-futuristic visual style inspired by 1980s electronic music, arcade graphics, and science-fiction imagery.",
            [
                "Neon grids",
                "Sunset gradients",
                "Chrome typography",
                "Purple-blue palettes",
            ],
            ["Earthy realism", "Vintage engraving", "Muted documentary photography"],
            ["1980s Retro", "Cyberpunk", "Vaporwave"],
        ),
    },
    "075": {
        "name": "Cyberpunk",
        **style(
            "High-tech dystopian imagery depicting dense technological environments and futuristic urban alienation.",
            ["Neon cities", "Cybernetic imagery", "Rainy streets", "Dense technology"],
            ["Pastoral landscapes", "Rustic simplicity", "Historical ornament"],
            ["Neo-Noir", "Glitch Art", "Industrial"],
        ),
    },
    "076": {
        "name": "Cyber Y2K",
        **style(
            "Glossy turn-of-the-millennium cyber aesthetics combining digital optimism with futuristic consumer design.",
            [
                "Chrome",
                "Translucent interfaces",
                "Iridescent surfaces",
                "Techno-organic forms",
            ],
            ["Aged paper", "Victorian ornament", "Muted pastoral colors"],
            ["Y2K", "Hyperreal CGI", "Vaporwave"],
        ),
    },
    "077": {
        "name": "Generative / Algorithmic Art",
        **style(
            "Visual compositions created or inspired by computational systems, procedural rules, and mathematical repetition.",
            [
                "Procedural patterns",
                "Complex repetition",
                "Algorithmic geometry",
                "Emergent structures",
            ],
            [
                "Arbitrary decoration",
                "Conventional portraits",
                "Handmade irregularity without structure",
            ],
            ["Scientific / Data Visualization", "Digital Abstraction", "Op Art"],
        ),
    },
    "078": {
        "name": "Digital Abstraction",
        **style(
            "Nonrepresentational digital imagery exploring virtual forms, synthetic colors, and computational textures.",
            [
                "Abstract 3D forms",
                "Synthetic gradients",
                "Digital layers",
                "Virtual surfaces",
            ],
            ["Literal storytelling", "Historical realism", "Conventional photography"],
            ["Generative / Algorithmic Art", "Glitch Art", "Hyperreal CGI"],
        ),
    },
    "079": {
        "name": "Hyperreal CGI",
        **style(
            "Highly polished computer-rendered imagery presenting artificial forms with convincing physical detail.",
            [
                "Photoreal materials",
                "Precise reflections",
                "Studio rendering",
                "Impossible objects",
            ],
            [
                "Low-resolution textures",
                "Rough handmade marks",
                "Unintentional artifacts",
            ],
            ["Cyber Y2K", "Digital Abstraction", "Surrealism"],
        ),
    },
    "080": {
        "name": "Digital Brutalism",
        **style(
            "Aggressive digital graphic language rejecting conventional interface beauty and polished visual hierarchy.",
            [
                "Harsh typography",
                "Raw interfaces",
                "Clashing colors",
                "Unrefined layouts",
            ],
            ["Elegant symmetry", "Luxury polish", "Decorative softness"],
            ["Anti-Design", "Brutalist Graphic Design", "Internet-Core"],
        ),
    },
    # ============================================================
    # 9. DARK, ATMOSPHERIC & CINEMATIC
    # ============================================================
    "081": {
        "name": "Film Noir",
        **style(
            "Classic cinematic darkness characterized by mystery, psychological tension, and dramatic monochromatic lighting.",
            ["Deep shadows", "Low-key lighting", "Urban silhouettes", "High contrast"],
            ["Bright cheerful palettes", "Flat illumination", "Playful graphics"],
            ["Black-and-White Editorial", "Cinematic Photography", "Neo-Noir"],
        ),
    },
    "082": {
        "name": "Neo-Noir",
        **style(
            "Contemporary reinterpretation of noir using expressive color, cinematic darkness, and urban atmosphere.",
            [
                "Colored shadows",
                "Night environments",
                "Dramatic reflections",
                "Moody lighting",
            ],
            ["Cheerful daylight", "Pastoral optimism", "Flat commercial photography"],
            ["Film Noir", "Cyberpunk", "Cinematic Photography"],
        ),
    },
    "083": {
        "name": "Dark Romanticism",
        **style(
            "Emotionally intense imagery exploring melancholy, mystery, beauty, and unsettling psychological themes.",
            [
                "Dramatic shadows",
                "Symbolic figures",
                "Melancholic landscapes",
                "Gothic details",
            ],
            [
                "Cheerful commercial colors",
                "Minimal emotional expression",
                "Playful cartoons",
            ],
            ["Gothic / Goth", "Symbolism", "Baroque"],
        ),
    },
    "084": {
        "name": "Ethereal / Dreamy",
        **style(
            "Soft atmospheric imagery creating a delicate, floating, otherworldly visual impression.",
            ["Soft focus", "Diffuse light", "Haze", "Pastel tones", "Gentle gradients"],
            ["Harsh edges", "Aggressive contrast", "Industrial textures"],
            ["Dreamcore", "Impressionism", "Magical Realism"],
        ),
    },
    "085": {
        "name": "Melancholic Minimalism",
        **style(
            "Restrained visual compositions expressing solitude, introspection, and emotional quietness.",
            ["Muted palette", "Isolated subjects", "Negative space", "Soft shadows"],
            ["Bright visual noise", "Dense ornament", "Playful exaggeration"],
            ["Minimalism", "Intimate Portraiture", "Ethereal / Dreamy"],
        ),
    },
    "086": {
        "name": "Horror / Macabre",
        **style(
            "Disturbing imagery centered on dread, mortality, uncanny figures, and psychologically unsettling scenes.",
            [
                "Grotesque forms",
                "Dark atmosphere",
                "Unsettling symbolism",
                "Distorted anatomy",
            ],
            [
                "Cheerful commercial imagery",
                "Lighthearted decoration",
                "Visual reassurance",
            ],
            ["Dark Fantasy", "Gothic / Goth", "Expressionism"],
        ),
    },
    "087": {
        "name": "Liminal Spaces",
        **style(
            "Empty transitional environments that feel simultaneously familiar, abandoned, and subtly unreal.",
            [
                "Empty corridors",
                "Vacant interiors",
                "Uncanny lighting",
                "Repetitive architecture",
            ],
            [
                "Crowded scenes",
                "Strong character narratives",
                "Overly decorative environments",
            ],
            ["Dreamcore", "Metaphysical Art", "Ethereal / Dreamy"],
        ),
    },
    "088": {
        "name": "Dystopian",
        **style(
            "Bleak visual environments suggesting oppressive systems, technological control, and societal decline.",
            [
                "Brutalist architecture",
                "Urban decay",
                "Harsh infrastructure",
                "Muted palettes",
            ],
            ["Optimistic advertising", "Pastoral beauty", "Playful decoration"],
            ["Industrial", "Cyberpunk", "Apocalyptic"],
        ),
    },
    "089": {
        "name": "Apocalyptic",
        **style(
            "Monumental imagery portraying destruction, collapse, devastation, or the aftermath of catastrophe.",
            [
                "Ruined cities",
                "Dramatic skies",
                "Desolate landscapes",
                "Monumental scale",
            ],
            ["Everyday comfort", "Cheerful palettes", "Commercial minimalism"],
            ["Dystopian", "Dark Fantasy", "Cinematic Photography"],
        ),
    },
    "090": {
        "name": "Dark Academia",
        **style(
            "Intellectual Gothic aesthetic inspired by classical learning, antique objects, and atmospheric historical interiors.",
            ["Old books", "Classical architecture", "Candlelight", "Deep brown tones"],
            ["Neon futurism", "Playful cartoons", "Modern commercial graphics"],
            ["Victorian / Antique", "Gothic / Goth", "Dark Romanticism"],
        ),
    },
    # ============================================================
    # 10. EXPERIMENTAL GRAPHIC DESIGN & COLLAGE
    # ============================================================
    "091": {
        "name": "Photocollage Aesthetic",
        **style(
            "Composite visual compositions combining photographic fragments into unexpected graphic relationships.",
            [
                "Cut photographs",
                "Overlapping imagery",
                "Juxtaposed scales",
                "Layered composition",
            ],
            [
                "Seamless single-shot realism",
                "Strict minimalism",
                "Uniform backgrounds",
            ],
            ["Mixed-Media Aesthetic", "Dada Collage", "Surrealism"],
        ),
    },
    "092": {
        "name": "Mixed-Media Aesthetic",
        **style(
            "Visually layered artwork combining multiple image-making traditions and contrasting surface qualities.",
            [
                "Illustration overlays",
                "Photographic fragments",
                "Textural contrast",
                "Handmade marks",
            ],
            [
                "Uniform digital polish",
                "Single-medium consistency",
                "Overly sterile layouts",
            ],
            ["Photocollage Aesthetic", "Grunge", "Postmodern Graphic Design"],
        ),
    },
    "093": {
        "name": "Dada Collage",
        **style(
            "Absurdist collage built from unexpected combinations of appropriated imagery, typography, and fragmented visual elements.",
            [
                "Found photographs",
                "Irregular typography",
                "Unexpected juxtapositions",
                "Visual discontinuity",
            ],
            ["Logical narratives", "Perfect alignment", "Conventional visual harmony"],
            ["Dadaism", "Photocollage Aesthetic", "Punk / DIY"],
        ),
    },
    "094": {
        "name": "Anti-Design",
        **style(
            "Intentional rejection of conventional graphic-design refinement through awkward, disruptive, or deliberately crude composition.",
            [
                "Unconventional typography",
                "Clashing colors",
                "Awkward spacing",
                "Visual tension",
            ],
            ["Corporate polish", "Perfect harmony", "Predictable layouts"],
            ["Digital Brutalism", "Punk / DIY", "Brutalist Graphic Design"],
        ),
    },
    "095": {
        "name": "New Wave Typography",
        **style(
            "Experimental typographic design breaking traditional modernist grids through dynamic and irregular arrangements.",
            [
                "Rotated text",
                "Irregular spacing",
                "Layered lettering",
                "Dynamic hierarchy",
            ],
            [
                "Rigid classical alignment",
                "Conventional book typography",
                "Static symmetry",
            ],
            ["Postmodern Graphic Design", "Deconstructivism", "Anti-Design"],
        ),
    },
    "096": {
        "name": "Postmodern Graphic Design",
        **style(
            "Eclectic visual design mixing historical references, appropriation, irony, and deliberately conflicting aesthetics.",
            [
                "Mixed typography",
                "Historical quotations",
                "Visual irony",
                "Layered references",
            ],
            [
                "Strict stylistic purity",
                "Predictable minimalism",
                "Uniform design systems",
            ],
            ["New Wave Typography", "Dada Collage", "Mixed-Media Aesthetic"],
        ),
    },
    "097": {
        "name": "Deconstructivism",
        **style(
            "Fragmented visual structures disrupting conventional spatial order, readability, and compositional stability.",
            [
                "Broken grids",
                "Displaced elements",
                "Angular fragmentation",
                "Layered text",
            ],
            ["Stable symmetry", "Traditional hierarchy", "Conventional realism"],
            ["New Wave Typography", "Cubism", "Noise / Experimental"],
        ),
    },
    "098": {
        "name": "Brutalist Graphic Design",
        **style(
            "Raw confrontational design emphasizing directness, structural simplicity, and deliberately uncompromising typography.",
            ["Heavy lettering", "Harsh contrast", "Raw layouts", "Oversized text"],
            ["Decorative elegance", "Soft gradients", "Luxury refinement"],
            ["Digital Brutalism", "Anti-Design", "Industrial"],
        ),
    },
    "099": {
        "name": "Maximalism",
        **style(
            "Dense visually abundant compositions emphasizing excess, complexity, ornament, and multiple simultaneous focal points.",
            [
                "Dense imagery",
                "Rich ornamentation",
                "Layered details",
                "Complex color relationships",
            ],
            [
                "Sparse composition",
                "Excessive negative space",
                "Severe visual restraint",
            ],
            ["Psychedelic Art", "Mixed-Media Aesthetic", "Baroque"],
        ),
    },
    "100": {
        "name": "Conceptual Graphic Art",
        **style(
            "Graphic compositions organized around a singular powerful visual idea or symbolic statement.",
            [
                "Visual metaphors",
                "Simple symbols",
                "Unexpected relationships",
                "Strong focal points",
            ],
            ["Decorative excess", "Random detail", "Conceptually unrelated imagery"],
            ["Minimalism", "Editorial Illustration", "Geometric Minimalism"],
        ),
    },
    # ============================================================
    # 11. ORGANIC, NATURAL & SPIRITUAL
    # ============================================================
    "101": {
        "name": "Botanical Art",
        **style(
            "Detailed or stylized representation of plants, flowers, foliage, and natural decorative structures.",
            ["Floral forms", "Leaves", "Botanical patterns", "Organic linework"],
            ["Mechanical imagery", "Harsh industrial geometry", "Digital glitches"],
            ["Art Nouveau", "Nature Romanticism", "Organic Abstraction"],
        ),
    },
    "102": {
        "name": "Organic Abstraction",
        **style(
            "Nonrepresentational artwork inspired by natural growth patterns, biological structures, and fluid forms.",
            [
                "Flowing shapes",
                "Cellular structures",
                "Soft contours",
                "Organic repetition",
            ],
            ["Strict mechanical geometry", "Hard industrial surfaces", "Rigid grids"],
            ["Biomorphic Surrealism", "Abstract Minimalism", "Botanical Art"],
        ),
    },
    "103": {
        "name": "Biomorphic Surrealism",
        **style(
            "Surreal compositions featuring imaginary organic forms that suggest living organisms and biological transformations.",
            [
                "Strange anatomy",
                "Fluid structures",
                "Hybrid organisms",
                "Organic distortions",
            ],
            ["Rigid geometric order", "Conventional realism", "Mechanical minimalism"],
            ["Surrealism", "Organic Abstraction", "Psychedelic Art"],
        ),
    },
    "104": {
        "name": "Nature Romanticism",
        **style(
            "Emotionally expressive depictions of natural environments emphasizing grandeur, atmosphere, and sublime beauty.",
            [
                "Dramatic landscapes",
                "Atmospheric skies",
                "Majestic mountains",
                "Natural light",
            ],
            ["Urban industrial clutter", "Artificial neon", "Flat corporate graphics"],
            ["Impressionism", "Magical Realism", "High Fantasy"],
        ),
    },
    "105": {
        "name": "Pastoral / Folk Art",
        **style(
            "Rustic handmade-looking visual identity inspired by traditional craft, rural culture, and vernacular illustration.",
            [
                "Folk patterns",
                "Rustic motifs",
                "Natural colors",
                "Decorative simplicity",
            ],
            ["Chrome CGI", "High-tech futurism", "Sterile luxury design"],
            ["Botanical Art", "1970s Vintage", "Naïve / Outsider Art"],
        ),
    },
    "106": {
        "name": "Eco-Futurism",
        **style(
            "Speculative future environments integrating ecological design, advanced technology, and natural systems.",
            [
                "Organic architecture",
                "Integrated vegetation",
                "Clean technology",
                "Natural materials",
            ],
            ["Polluted dystopia", "Industrial decay", "Purely mechanical environments"],
            ["Organic Abstraction", "Hyperreal CGI", "Nature Romanticism"],
        ),
    },
    "107": {
        "name": "Sacred Geometry",
        **style(
            "Symbolic visual compositions built around mathematical symmetry and geometries associated with spiritual traditions.",
            [
                "Mandala structures",
                "Concentric patterns",
                "Geometric symmetry",
                "Repeated symbols",
            ],
            ["Chaotic randomness", "Unstructured imagery", "Irregular visual noise"],
            ["Visionary Art", "Geometric Minimalism", "Mysticism / Occult Art"],
        ),
    },
    "108": {
        "name": "Mysticism / Occult Art",
        **style(
            "Esoteric symbolic imagery referencing hidden knowledge, ritual traditions, and metaphysical belief systems.",
            [
                "Arcane symbols",
                "Ritual objects",
                "Celestial diagrams",
                "Mystical figures",
            ],
            [
                "Commercial advertising",
                "Playful realism",
                "Conventional corporate design",
            ],
            ["Symbolism", "Sacred Geometry", "Dark Romanticism"],
        ),
    },
    "109": {
        "name": "Celestial Art",
        **style(
            "Astronomical and cosmic imagery emphasizing celestial bodies, space, and symbolic representations of the universe.",
            [
                "Stars",
                "Planets",
                "Astronomical diagrams",
                "Nebulae",
                "Celestial patterns",
            ],
            [
                "Ordinary urban scenes",
                "Crowded commercial graphics",
                "Unrelated decorative clutter",
            ],
            ["Cosmic Surrealism", "Sacred Geometry", "Visionary Art"],
        ),
    },
    "110": {
        "name": "New Age / Spiritual Art",
        **style(
            "Meditative and transcendent imagery expressing spiritual harmony, inner awareness, and universal interconnectedness.",
            [
                "Luminous energy",
                "Soft gradients",
                "Meditative figures",
                "Cosmic symbolism",
            ],
            [
                "Aggressive industrial imagery",
                "Harsh visual tension",
                "Cynical commercial graphics",
            ],
            ["Visionary Art", "Ethereal / Dreamy", "Celestial Art"],
        ),
    },
    # ============================================================
    # 12. CONTEMPORARY CULTURAL & FASHION AESTHETICS
    # ============================================================
    "111": {
        "name": "Luxury Editorial",
        **style(
            "Sophisticated high-end visual direction emphasizing refinement, exclusivity, and carefully controlled aesthetics.",
            [
                "Elegant typography",
                "Refined lighting",
                "Restrained palettes",
                "Premium materials",
            ],
            ["Grunge textures", "Clashing colors", "Uncontrolled visual clutter"],
            ["Fashion Editorial", "Art Deco", "Minimalism"],
        ),
    },
    "112": {
        "name": "Avant-Garde Fashion",
        **style(
            "Experimental fashion-centered imagery exploring unconventional silhouettes, identity, and sculptural styling.",
            [
                "Unusual garments",
                "Sculptural silhouettes",
                "Conceptual poses",
                "Experimental styling",
            ],
            [
                "Conventional commercial fashion",
                "Casual styling",
                "Predictable portraiture",
            ],
            ["Fashion Editorial", "Surrealism", "Luxury Editorial"],
        ),
    },
    "113": {
        "name": "Hyperpop",
        **style(
            "Exaggerated digital aesthetic emphasizing artificiality, visual excess, synthetic color, and internet-era pop culture.",
            [
                "Extreme saturation",
                "Digital distortion",
                "Glossy artificial surfaces",
                "Bold graphics",
            ],
            [
                "Naturalistic restraint",
                "Muted historical palettes",
                "Traditional elegance",
            ],
            ["Cyber Y2K", "Neo-Pop", "Glitch Art"],
        ),
    },
    "114": {
        "name": "Internet-Core",
        **style(
            "Eclectic visual identity drawing from online subcultures, memes, browser interfaces, and digital nostalgia.",
            ["Retro interfaces", "Web graphics", "Digital icons", "Fragmented layouts"],
            [
                "Classical composition",
                "Historical authenticity",
                "Uniform polished branding",
            ],
            ["Vaporwave", "Pixel Art", "Digital Brutalism"],
        ),
    },
    "115": {
        "name": "Kawaii",
        **style(
            "Cute playful aesthetic emphasizing friendly characters, simplified shapes, and emotionally warm imagery.",
            [
                "Pastel colors",
                "Rounded forms",
                "Cute characters",
                "Playful expressions",
            ],
            [
                "Threatening realism",
                "Harsh industrial palettes",
                "Dark psychological imagery",
            ],
            ["Cartoon Art", "Manga / Anime", "Neo-Pop"],
        ),
    },
    "116": {
        "name": "Harajuku / Decora",
        **style(
            "Colorful Japanese street-fashion aesthetic emphasizing playful layering, eccentric accessories, and expressive individuality.",
            [
                "Bright colors",
                "Layered accessories",
                "Pattern mixing",
                "Playful styling",
            ],
            ["Understated minimalism", "Conservative fashion", "Muted monochrome"],
            ["Kawaii", "Neo-Pop", "Maximalism"],
        ),
    },
    "117": {
        "name": "Afrofuturism",
        **style(
            "Speculative aesthetic combining African diasporic cultural references with science fiction, technology, and imagined futures.",
            [
                "Speculative fashion",
                "Futuristic architecture",
                "Cultural symbolism",
                "Technological imagery",
            ],
            [
                "Generic sci-fi without cultural context",
                "Stereotypical symbols",
                "Historical reductionism",
            ],
            ["Cyberpunk", "Visionary Art", "Avant-Garde Fashion"],
        ),
    },
    "118": {
        "name": "Afrosurrealism",
        **style(
            "Surreal visual language exploring Black identity, cultural experience, history, and alternative interpretations of reality.",
            [
                "Unexpected symbolism",
                "Dream logic",
                "Cultural references",
                "Conceptual portraiture",
            ],
            [
                "Generic surrealism without context",
                "Cultural stereotypes",
                "Literal conventional realism",
            ],
            ["Surrealism", "Conceptual Photography", "Symbolism"],
        ),
    },
    "119": {
        "name": "Contemporary Streetwear",
        **style(
            "Fashion-oriented urban aesthetic emphasizing bold branding, youth identity, and contemporary street culture.",
            ["Graphic logos", "Urban fashion", "Bold lettering", "Street photography"],
            [
                "Antique ornamentation",
                "Formal classical styling",
                "Excessively delicate imagery",
            ],
            ["Underground Street Art", "Neo-Pop", "Fashion Editorial"],
        ),
    },
    "120": {
        "name": "Indie Sleaze",
        **style(
            "Messy nightlife aesthetic inspired by 2000s indie culture, party photography, and deliberately unpolished glamour.",
            [
                "Direct flash",
                "Nightlife scenes",
                "Casual fashion",
                "Grainy photographs",
            ],
            ["Luxury retouching", "Controlled studio perfection", "Sterile minimalism"],
            ["Analog Nostalgia", "1990s Nostalgia", "Lo-Fi Indie"],
        ),
    },
}
