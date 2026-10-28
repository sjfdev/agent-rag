from langchain_community.document_loaders import PyPDFLoader, DirectoryLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from src.engine import kb_engine
import os

class EnterpriseETL:
    def __init__(self, data_dir: str = "./data"):
        self.data_dir = data_dir
        # Enterprise-grade splitting: balancing context and granularity
        self.splitter = RecursiveCharacterTextSplitter(
            chunk_size=800, 
            chunk_overlap=100,
            separators=["\n\n", "\n", " ", ""]
        )

    def process_department_folder(self, dept_name: str, folder_path: str):
        """
        Loads all PDFs from a folder and tags them with a department for permission control.
        """
        print(f"Processing documents for {dept_name} in {folder_path}...")
        
        # Load PDFs from the directory
        loader = DirectoryLoader(folder_path, glob="*.pdf", loader_cls=PyPDFLoader)
        raw_docs = loader.load()
        
        # Split into chunks
        split_docs = self.splitter.split_documents(raw_docs)
        
        texts = [doc.page_content for doc in split_docs]
        # Add department metadata to every chunk
        metadatas = [{"dept": dept_name, "source": doc.metadata.get("source")} for doc in split_docs]
        
        # Batch ingest into Milvus
        kb_engine.add_documents(texts, metadatas)
        print(f"Successfully ingested {len(texts)} chunks for {dept_name}.")

# Example usage script
if __name__ == "__main__":
    etl = EnterpriseETL()
    # Structure: data/HR/*.pdf, data/Finance/*.pdf, etc.
    os.makedirs("./data/HR", exist_ok=True)
    os.makedirs("./data/Finance", exist_ok=True)
    
    # Example: etl.process_department_folder("HR", "./data/HR")
